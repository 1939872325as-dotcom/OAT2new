# OCR 双引擎池：CPU + GPU(DML) 同时计算，按实测时延动态分配负载
#
# 背景：OCRManager 原来只有单个 RapidOCR 实例（纯 CPU），整条 det→cls→rec
# 管线都压在一颗 CPU 上，长跑时 CPU 占用高、单帧多张图的 OCR 串行排队。
# 本池子起两个引擎实例：
#   - cpu 引擎：CPUExecutionProvider（一直可用）
#   - gpu 引擎：DmlExecutionProvider（onnxruntime-directml，Win10+ / RTX 等独显）
# 每个引擎一条常驻工作线程，请求经调度器分发，两边可真正同时计算。
#
# 动态分配（least-work + EWMA 实测时延）：
#   调度分 = 该引擎待处理任务数 × 指数滑动平均单次耗时。
#   每次把新请求发给调度分最小的引擎。于是：
#   - GPU 更快 → 它的调度分涨得慢 → 自然多扛任务（CPU 开销下降）；
#   - GPU 卡顿/出错 → 标记不健康进入冷却，任务全部回落 CPU，恢复后自动试探回归；
#   - 分配比例随实测时延自动漂移，不需要手工配权重。
#
# 环境变量：
#   OAT_OCR_MODE = auto(默认，检测到 GPU provider 就双引擎) / cpu(强制单CPU) / dual(强制尝试GPU)
#
# 线程模型：RapidOCR 实例只在创建它的线程里被调用（每引擎单工作线程串行消费），
# 多线程调用方（pipeline / GUI）通过 infer() 提交，天然并发安全。
import os
import queue
import threading
import time

import numpy as np

from OAT.utils.logging import logger


def _env_mode() -> str:
    mode = (os.environ.get("OAT_OCR_MODE") or "auto").strip().lower()
    return mode if mode in ("auto", "cpu", "dual") else "auto"


def _gpu_provider_available() -> bool:
    """检测当前 onnxruntime 是否带 GPU 执行后端（DML / CUDA）。"""
    try:
        import onnxruntime as ort
        eps = ort.get_available_providers()
        return any(ep in eps for ep in ("DmlExecutionProvider", "CUDAExecutionProvider"))
    except Exception:
        return False

# 单次 OCR 推理的挂死判定（秒）：整帧实测 0.15~0.5s，20s 已是几十倍余量；
# 超时就重新派发（原 60s：引擎偶发挂死时会把「偶发性不识别」放大成整分钟卡死）。
_INFER_TIMEOUT = 20.0


class _Task:
    """一次推理请求：图片 + 每调用参数 + 完成事件 + 结果槽。"""

    __slots__ = ("img", "kwargs", "event", "result", "error")

    def __init__(self, img, kwargs=None):
        self.img = img
        self.kwargs = kwargs or {}
        self.event = threading.Event()
        self.result = None
        self.error = None


class _EngineWorker:
    """单个引擎 + 常驻消费线程。引擎实例只在本线程内创建和调用。"""

    def __init__(self, name: str, params: dict):
        self.name = name
        self.params = params
        self.queue: "queue.SimpleQueue[_Task]" = queue.SimpleQueue()
        self.lock = threading.Lock()
        self.ewma: float = 0.0          # 单次推理 EWMA 耗时（秒），0=尚无实测
        self._pending_count = 0         # 已提交未完成数（含正在跑的 1 个）
        self.calls = 0
        self.errors = 0
        self.healthy = True
        self.cooldown_until = 0.0       # 出错后冷却，到期且空闲时才允许再试探
        self.perm_dead = False          # 引擎建不起来（无 GPU / 模型缺失）→ 永久出局
        self.warmup_error = None
        self.warmup_done = threading.Event()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name=f"OCR-{name}")

    # ---- 生命周期 ----

    def start(self):
        self.thread.start()

    def _run(self):
        try:
            from rapidocr import RapidOCR
            engine = RapidOCR(params=self.params)
            # 预热：触发模型加载（DML 首跑还有着色器编译），之后正式请求都是热路径。
            # 64x64 白图 det 检不出框是正常返回，不是错误。
            warm = np.full((64, 64, 3), 255, dtype=np.uint8)
            engine(warm)
            logger.info(f"OCR引擎[{self.name}] 就绪")
        except Exception as e:
            self.perm_dead = True
            self.healthy = False
            self.warmup_error = str(e)
            logger.warning(f"OCR引擎[{self.name}] 初始化失败，本会话只用其余引擎: {e}")
            return
        finally:
            self.warmup_done.set()

        while True:
            task = self.queue.get()
            t0 = time.perf_counter()
            try:
                task.result = engine(task.img, **task.kwargs)
                task.error = None
                cost = time.perf_counter() - t0
                with self.lock:
                    self.calls += 1
                    # EWMA 平滑系数 0.25：十几轮就能贴住真实时延，单次抖动不翻车
                    self.ewma = cost if self.ewma == 0.0 else \
                        0.75 * self.ewma + 0.25 * cost
                    self.healthy = True
            except Exception as e:
                task.error = e
                with self.lock:
                    self.errors += 1
                    self.healthy = False
                    # GPU 偶发驱动抽风：冷却 30s 后用空闲试探回归；CPU 挂了更短冷却
                    self.cooldown_until = time.monotonic() + \
                        (30.0 if self.name == "gpu" else 5.0)
                logger.warning(f"OCR引擎[{self.name}] 推理失败，进入冷却: {e}")
            finally:
                with self.lock:
                    self._pending_count -= 1
                task.event.set()

    # ---- 状态与调度分 ----

    def score(self, now: float) -> float:
        """调度分：待处理数 × 实测时延。cooling/未热身的引擎返回 inf（不派新活）。

        未热身也必须避开：CUDA EP 初始化窗口内（着色器编译/上下文创建）收到的
        推理请求会底层空转返回空 det——表现为「明明有字却识别为空」（2026-11-16
        实测：exe 首次截图添加时 GPU 未热身，'准备' 识别为空）。"""
        with self.lock:
            if self.perm_dead or not self.warmup_done.is_set():
                return float("inf")
            if not self.healthy:
                if now < self.cooldown_until:
                    return float("inf")
                # 冷却已过但还压着活就继续避开，空闲时才允许试探回归
                if self._pending_count > 0:
                    return float("inf")
            # 尚无实测数据时给中性 seed（1.0），并列时调度器按引擎顺序偏向 gpu
            return self._pending_count * (self.ewma if self.ewma > 0.0 else 1.0)

    def usable(self) -> bool:
        with self.lock:
            return not self.perm_dead

    def submit(self, task: _Task):
        with self.lock:
            self._pending_count += 1
        self.queue.put(task)


class OCREnginePool:
    """CPU+GPU 双引擎 OCR 池。infer(img) 语义与 RapidOCR(img) 一致（同步返回输出对象）。"""

    def __init__(self, base_params: dict):
        self._workers: list = []
        self._dispatch_lock = threading.Lock()
        self._build(base_params)

    def _build(self, base_params: dict):
        mode = _env_mode()
        cpu_params = dict(base_params)

        want_gpu = mode == "dual" or (mode == "auto" and _gpu_provider_available())
        if mode == "dual" and not _gpu_provider_available():
            logger.warning("OAT_OCR_MODE=dual 但当前 onnxruntime 不带 GPU provider"
                           "（需安装 onnxruntime-directml），回落单 CPU 引擎")

        if want_gpu:
            # 双引擎时把 CPU 引擎的算力线程压到一半：GPU 分担后单引擎不必满核轰炸，
            # 这是"开销分配"的一部分——总量守恒下让 GPU 多担。
            cores = os.cpu_count() or 4
            cpu_params.setdefault("EngineConfig.onnxruntime.intra_op_num_threads",
                                  max(2, cores // 2))
            gpu_params = dict(base_params)
            # 环境里装的是 onnxruntime-directml → DML；装了 onnxruntime-gpu 则走 CUDA
            try:
                import onnxruntime as ort
                if "CUDAExecutionProvider" in ort.get_available_providers():
                    gpu_params["EngineConfig.onnxruntime.use_cuda"] = True
                    gpu_name = "gpu(cuda)"
                else:
                    gpu_params["EngineConfig.onnxruntime.use_dml"] = True
                    gpu_name = "gpu(dml)"
            except Exception:
                gpu_params["EngineConfig.onnxruntime.use_dml"] = True
                gpu_name = "gpu(dml)"
            gpu_params.setdefault("EngineConfig.onnxruntime.intra_op_num_threads", 1)
            # 引擎列表顺序 [gpu, cpu]：调度分并列时 min() 取前者 → 首选卸载到 GPU
            self._workers.append(_EngineWorker("gpu", gpu_params))
            logger.info(f"OCR双引擎模式：CPU + {gpu_name}，动态分配负载")

        self._workers.append(_EngineWorker("cpu", cpu_params))
        for w in self._workers:
            w.start()

        # 等两个引擎并行预热完成（各自线程里做，耗时 ≈ max 而不是 sum）。
        # 注意只能等 warmup_done 事件，不能 join 线程——工作线程是常驻死循环。
        deadline = time.monotonic() + 120.0
        for w in self._workers:
            w.warmup_done.wait(max(0.1, deadline - time.monotonic()))
        alive = [w.name for w in self._workers if w.usable()]
        dead = [f"{w.name}({w.warmup_error})" for w in self._workers if not w.usable()]
        logger.info(f"OCR引擎池初始化完成：可用={alive}"
                    + (f"；不可用={dead}" if dead else ""))
        if not alive:
            raise RuntimeError("OCR 引擎池初始化失败：无任何可用引擎")

    # ---- 对外 API ----

    def __call__(self, img, **kwargs):
        """让池子可直接 pool(img) 调用，替代原 RapidOCR 实例的调用习惯
        （huijuanshuafen 等模块里有 mgr.reader(shot) 的直接调用，保持兼容）。
        kwargs 透传给底层 RapidOCR.__call__（如 box_thresh / unclip_ratio）。"""
        return self.infer(img, **kwargs)

    def infer(self, img, **kwargs):
        """同步推理，等价于原来的 reader(img)。内部动态选引擎，超时自动换道重试一次。

        kwargs 每调用透传（box_thresh/unclip_ratio/text_score 等）；RapidOCR 3.7+
        的 __call__ 都支持这些覆盖键（已核对源码）。
        """
        task = self._dispatch(img, kwargs)
        if not task.event.wait(timeout=_INFER_TIMEOUT):
            # 引擎疑似挂死（20s 无响应）：不再等，重新派一次（挂死引擎在途数没清零，
            # 调度分会自动避开它；若另一引擎也不可用则只能继续等）。
            # 20s 的依据：整帧 OCR 实测 0.15~0.5s，20s 已是 40 倍余量；
            # 60s 会让「偶发性不识别」看起来像卡死（实测 11:01:04 挂过一次）。
            logger.warning(f"OCR引擎推理超时({_INFER_TIMEOUT:g}s)，重新派发")
            task = self._dispatch(img, kwargs)
            if not task.event.wait(timeout=90.0):
                return None
        if task.error is not None:
            return None
        return task.result

    def stats(self) -> dict:
        """当前分配状态，供日志/调试：各引擎调用数、EWMA 时延、在途数、健康度。"""
        out = {}
        for w in self._workers:
            with w.lock:
                out[w.name] = {
                    "calls": w.calls, "errors": w.errors,
                    "ewma_ms": round(w.ewma * 1000.0, 1) if w.ewma else None,
                    "pending": w._pending_count,
                    "healthy": w.healthy and not w.perm_dead,
                }
        return out

    def stats_line(self) -> str:
        s = self.stats()
        return " | ".join(
            f"{k}: {v['calls']}次 ewma={v['ewma_ms']}ms 在途{v['pending']}"
            f"{'' if v['healthy'] else '(冷却中)'}"
            for k, v in s.items())

    # ---- 内部 ----

    def _dispatch(self, img, kwargs=None) -> _Task:
        with self._dispatch_lock:
            now = time.monotonic()
            scored = [(w.score(now), w) for w in self._workers]
            best_score, best = min(scored, key=lambda p: p[0])
            if best_score == float("inf"):
                # 兜底顺序：已热身的引擎优先（哪怕在冷却），全没热身才派给
                # 未热身引擎排队等它就绪——比把请求丢给还在初始化的 GPU 强，
                # 后者会返回空结果而不是等好再算。
                ready = [w for w in self._workers
                         if w.warmup_done.is_set() and not w.perm_dead]
                pool = ready if ready else [w for w in self._workers if not w.perm_dead]
                best = (min(pool, key=lambda w: w.calls) if pool
                        else min(self._workers, key=lambda w: w.calls))
            task = _Task(img, kwargs)
            best.submit(task)
            return task
