using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Management;
using System.Windows.Forms;

namespace MySOP.Core
{
    /// <summary>从旧的三件套（Snipaste / Umi-OCR / AHK 脚本）迁移过来。</summary>
    public static class Migration
    {
        static readonly string[] OldStartupLinks = { "Snipaste.lnk", "Umi-OCR.lnk", "Reference - Shortcut.lnk" };

        static string StartupDir { get { return Environment.GetFolderPath(Environment.SpecialFolder.Startup); } }
        static string BackupDir { get { return Path.Combine(Host.AppDir, "backup", "startup"); } }

        public static void Run(Action reload)
        {
            var r = MessageBox.Show(
                "将会：\n\n" +
                "1. 关闭正在运行的 Snipaste、Umi-OCR、Reference.ahk，把热键让给本程序\n" +
                "2. 把它们的开机启动快捷方式移到 backup\\startup 文件夹（随时可以恢复）\n" +
                (Host.Embedded ? "3. 以后由「我的 AI Bot」开机时一起启动\n\n" : "3. 设置本程序开机自启\n\n") +
                "软件本身不会被卸载或删除。继续吗？",
                Host.AppName + " — 接管旧软件", MessageBoxButtons.OKCancel, MessageBoxIcon.Question);
            if (r != DialogResult.OK) return;

            string report = "";
            Directory.CreateDirectory(BackupDir);
            foreach (var name in OldStartupLinks)
            {
                string src = Path.Combine(StartupDir, name);
                if (!File.Exists(src)) continue;
                string dst = Path.Combine(BackupDir, name);
                if (File.Exists(dst)) File.Delete(dst);
                File.Move(src, dst);
                report += "已移除开机启动：" + name + "\n";
            }

            report += StopProcess("Umi-OCR", null);
            report += StopAhk();
            report += StopSnipaste();
            if (!Host.Embedded)
            {
                Host.AutoStart = true;
                report += "已设置本程序开机自启\n";
            }

            System.Threading.Thread.Sleep(800);
            reload();
            MessageBox.Show(report, Host.AppName, MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        public static void Restore()
        {
            if (!Directory.Exists(BackupDir)) { Host.Notify("没有可恢复的项目", "backup\\startup 为空"); return; }
            int n = 0;
            foreach (var f in Directory.GetFiles(BackupDir, "*.lnk"))
            {
                string dst = Path.Combine(StartupDir, Path.GetFileName(f));
                if (!File.Exists(dst)) { File.Move(f, dst); n++; }
            }
            Host.Notify("已恢复 " + n + " 个开机启动项", "下次开机时旧软件会照常启动。");
        }

        static string StopProcess(string name, string commandLineContains)
        {
            string msg = "";
            foreach (var p in Process.GetProcessesByName(name))
            {
                try
                {
                    // 连同子进程（Umi-OCR 的 python/引擎）一起结束
                    var psi = new ProcessStartInfo("taskkill", "/T /F /PID " + p.Id) { CreateNoWindow = true, UseShellExecute = false };
                    Process.Start(psi).WaitForExit(5000);
                    msg += "已关闭 " + name + "\n";
                }
                catch (Exception ex) { msg += "关闭 " + name + " 失败：" + ex.Message + "\n"; }
            }
            return msg;
        }

        static string StopAhk()
        {
            string msg = "";
            try
            {
                using (var s = new ManagementObjectSearcher("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name LIKE 'AutoHotkey%'"))
                    foreach (ManagementObject o in s.Get())
                    {
                        string cmd = Convert.ToString(o["CommandLine"]) ?? "";
                        if (cmd.IndexOf("Reference.ahk", StringComparison.OrdinalIgnoreCase) < 0) continue;
                        Process.GetProcessById(Convert.ToInt32(o["ProcessId"])).Kill();
                        msg += "已关闭 AHK 脚本 Reference.ahk\n";
                    }
            }
            catch (Exception ex) { msg += "关闭 AHK 失败：" + ex.Message + "\n"; }
            return msg;
        }

        static string StopSnipaste()
        {
            var procs = Process.GetProcessesByName("Snipaste");
            if (procs.Length == 0) return "";
            foreach (var p in procs)
            {
                try { p.Kill(); p.WaitForExit(3000); }
                catch
                {
                    // Snipaste 以管理员身份运行时无法直接结束，改用它自带的退出命令
                    try
                    {
                        string exe = @"C:\Program Files (x86)\Snipaste\Snipaste.exe";
                        if (File.Exists(exe)) Process.Start(exe, "exit").WaitForExit(5000);
                    }
                    catch { }
                }
            }
            return Process.GetProcessesByName("Snipaste").Any()
                ? "Snipaste 以管理员身份运行，没能关闭：请在它的托盘图标上右键 → 退出，然后托盘右键本程序 → 设置 → 重新加载配置\n"
                : "已关闭 Snipaste\n";
        }
    }
}
