using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

static class Program
{
    [STAThread]
    static void Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string appDir = Path.Combine(root, "app");
        string exe = Path.Combine(appDir, "OAT2.exe");
        if (!File.Exists(exe))
        {
            MessageBox.Show(
                @"找不到 app\OAT2.exe。请先把压缩包完整解压，再运行本程序。",
                "OAT2 启动器", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        try
        {
            Process.Start(new ProcessStartInfo(exe)
            {
                WorkingDirectory = appDir,
                UseShellExecute = true
            });
        }
        catch (Exception ex)
        {
            MessageBox.Show("启动失败：" + ex.Message, "OAT2 启动器",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
