using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Windows.Forms;
using Microsoft.Win32;

namespace MySOP.Core
{
    /// <summary>全局服务入口：配置、热键、命令、日志、通知、跨模块服务。</summary>
    public static class Host
    {
        public const string AppName = "我的 SOP";
        public static string AppDir;
        /// <summary>由 AI Bot 启动（--embedded）：不显示自己的托盘图标，菜单在 Bot 的托盘里。</summary>
        public static bool Embedded;
        const string AiBotWindow = "MyAIBot_IPC";
        public static Config Config;
        public static HotkeyManager Hotkeys;
        public static Control Ui;                       // 用于把工作切回 UI 线程
        public static readonly Dictionary<string, CommandInfo> Commands = new Dictionary<string, CommandInfo>(StringComparer.OrdinalIgnoreCase);
        static readonly Dictionary<Type, object> services = new Dictionary<Type, object>();
        static readonly object logLock = new object();

        public static void Provide<T>(T service) where T : class { services[typeof(T)] = service; }
        public static T Get<T>() where T : class
        {
            object o;
            return services.TryGetValue(typeof(T), out o) ? (T)o : null;
        }

        public static bool RunCommand(string name)
        {
            CommandInfo c;
            if (!Commands.TryGetValue(name.Trim(), out c)) { Log("未知命令: " + name); return false; }
            try { c.Run(); }
            catch (Exception ex) { Log("命令 " + name + " 出错: " + ex); Notify("出错了", ex.Message); }
            return true;
        }

        public static void Log(string msg)
        {
            try
            {
                lock (logLock)
                {
                    string dir = Path.Combine(AppDir, "logs");
                    Directory.CreateDirectory(dir);
                    string file = Path.Combine(dir, "sop.log");
                    if (File.Exists(file) && new FileInfo(file).Length > 1024 * 1024) File.Delete(file);
                    File.AppendAllText(file, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss ") + msg + Environment.NewLine, Encoding.UTF8);
                }
            }
            catch { }
        }

        public static void Notify(string title, string text, Action onClick = null)
        {
            if (Ui == null) return;
            if (Ui.InvokeRequired) { Ui.BeginInvoke(new Action(() => Notify(title, text, onClick))); return; }
            Toast.Show(title, text, onClick);
        }

        public static void OnUi(Action a)
        {
            if (Ui.InvokeRequired) Ui.BeginInvoke(a); else a();
        }

        // ---------- 发给 AI Bot 面板 ----------
        /// <summary>把图片交给 AI Bot（存成临时 PNG，面板里预填“请看这张截图”）。</summary>
        public static void AskAiImage(Bitmap bmp)
        {
            string dir = Path.Combine(Path.GetTempPath(), "MyAIBot");
            Directory.CreateDirectory(dir);
            foreach (var old in Directory.GetFiles(dir, "shot_*.png"))
                if (File.GetCreationTime(old) < DateTime.Now.AddDays(-3)) try { File.Delete(old); } catch { }
            string path = Path.Combine(dir, "shot_" + DateTime.Now.ToString("yyyyMMdd_HHmmss_fff") + ".png");
            bmp.Save(path, System.Drawing.Imaging.ImageFormat.Png);
            SendToAi("{\"cmd\":\"ask_image\",\"path\":" + Config.ToAsciiJsonString(path) + "}");
        }

        public static void AskAiText(string text)
        {
            SendToAi("{\"cmd\":\"ask_text\",\"text\":" + Config.ToAsciiJsonString(text) + "}");
        }

        static void SendToAi(string json)
        {
            if (!HotkeyManager.SendTo(AiBotWindow, json))
                Notify("AI Bot 没有在运行", "先启动「我的 AI Bot」");
        }

        // ---------- 开机自启（HKCU Run） ----------
        const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
        const string RunName = "MySOP";

        public static bool AutoStart
        {
            get
            {
                using (var k = Registry.CurrentUser.OpenSubKey(RunKey))
                    return k != null && k.GetValue(RunName) != null;
            }
            set
            {
                using (var k = Registry.CurrentUser.OpenSubKey(RunKey, true))
                {
                    if (value) k.SetValue(RunName, "\"" + Application.ExecutablePath + "\"");
                    else k.DeleteValue(RunName, false);
                }
            }
        }
    }

    /// <summary>托盘程序本体：加载模块、构建菜单。</summary>
    public class AppHost : ApplicationContext
    {
        readonly NotifyIcon tray;
        readonly List<IModule> modules = new List<IModule>();
        readonly Func<IEnumerable<IModule>> moduleFactory;
        readonly Control uiMarshal;

        public AppHost(Func<IEnumerable<IModule>> moduleFactory)
        {
            this.moduleFactory = moduleFactory;
            uiMarshal = new Control();
            uiMarshal.CreateControl();
            var h = uiMarshal.Handle; // 强制创建句柄
            Host.Ui = uiMarshal;
            Host.Hotkeys = new HotkeyManager();
            Host.Hotkeys.CommandReceived += cmd => Host.RunCommand(cmd);

            tray = new NotifyIcon { Icon = IconFactory.Create(32), Text = Host.AppName, Visible = !Host.Embedded };
            tray.MouseClick += (s, e) => { if (e.Button == MouseButtons.Left) Host.RunCommand("capture"); };

            LoadModules();
            var failed = Host.Hotkeys.All.Where(x => !x.Ok && x.Error != "未设置").ToList();
            foreach (var f in failed) Host.Log("热键注册失败 " + f.Combo + " " + f.Description + "：" + f.Error);
            if (failed.Count > 0)
                Host.Notify("有 " + failed.Count + " 个热键注册失败",
                    string.Join("\n", failed.Select(f => f.Combo + "  " + f.Description + "：" + f.Error)) +
                    "\n\n多半是旧软件（Snipaste / Umi-OCR / AHK）还在运行。" +
                    (Host.Embedded ? "Bot 托盘右键 → 截图与识字 → 接管旧软件。" : "右键托盘 → 接管旧软件。"),
                    () => HotkeyListForm.ShowList());
        }

        void LoadModules()
        {
            Host.Commands.Clear();
            var contexts = new List<KeyValuePair<IModule, ModuleContext>>();
            var enabled = Host.Config.Section("modules");
            foreach (var m in moduleFactory().Concat(LoadPlugins()))
            {
                enabled.Default(m.Id, true);
                if (!enabled.Bool(m.Id, true)) continue;
                try
                {
                    var ctx = new ModuleContext(m);
                    m.Init(ctx);
                    modules.Add(m);
                    contexts.Add(new KeyValuePair<IModule, ModuleContext>(m, ctx));
                }
                catch (Exception ex)
                {
                    Host.Log("模块 " + m.Id + " 初始化失败: " + ex);
                    Host.Notify("模块加载失败：" + m.Name, ex.Message);
                }
            }
            RegisterCoreCommands();
            Host.Config.SaveIfDirty();
            BuildMenu(contexts);
        }

        static IEnumerable<IModule> LoadPlugins()
        {
            var result = new List<IModule>();
            string dir = Path.Combine(Host.AppDir, "plugins");
            if (!Directory.Exists(dir)) return result;
            foreach (var dll in Directory.GetFiles(dir, "*.dll"))
            {
                try
                {
                    foreach (var t in Assembly.LoadFrom(dll).GetTypes())
                        if (typeof(IModule).IsAssignableFrom(t) && !t.IsAbstract && t.GetConstructor(Type.EmptyTypes) != null)
                            result.Add((IModule)Activator.CreateInstance(t));
                }
                catch (Exception ex) { Host.Log("插件加载失败 " + dll + ": " + ex); }
            }
            return result;
        }

        void RegisterCoreCommands()
        {
            Host.Commands["reload"] = new CommandInfo { Name = "reload", Description = "重新加载配置", Module = "核心", Run = Reload };
            Host.Commands["quit"] = new CommandInfo { Name = "quit", Description = "退出", Module = "核心", Run = Quit };
            Host.Commands["hotkeys"] = new CommandInfo { Name = "hotkeys", Description = "热键一览", Module = "核心", Run = HotkeyListForm.ShowList };
            Host.Commands["migrate"] = new CommandInfo { Name = "migrate", Description = "接管旧软件", Module = "核心", Run = () => Migration.Run(Reload) };
            Host.Commands["migrate-restore"] = new CommandInfo { Name = "migrate-restore", Description = "恢复旧软件的开机启动", Module = "核心", Run = Migration.Restore };
            Host.Commands["edit-config"] = new CommandInfo { Name = "edit-config", Description = "编辑配置", Module = "核心", Run = () => OpenFile(Host.Config.Path) };
        }

        void BuildMenu(List<KeyValuePair<IModule, ModuleContext>> contexts)
        {
            var menu = new ContextMenuStrip();
            menu.Font = new Font("Microsoft YaHei UI", 9f);
            var title = new ToolStripMenuItem(Host.AppName) { Enabled = false };
            menu.Items.Add(title);
            menu.Items.Add(new ToolStripSeparator());
            foreach (var kv in contexts)
            {
                if (kv.Value.MenuItems.Count == 0) continue;
                foreach (var item in kv.Value.MenuItems) menu.Items.Add(item);
                menu.Items.Add(new ToolStripSeparator());
            }

            menu.Items.Add(new ToolStripMenuItem("热键一览…", null, (s, e) => HotkeyListForm.ShowList()));
            var settings = new ToolStripMenuItem("设置");
            settings.DropDownItems.Add("编辑配置 config.json", null, (s, e) => OpenFile(Host.Config.Path));
            settings.DropDownItems.Add("重新加载配置", null, (s, e) => Reload());
            settings.DropDownItems.Add("打开程序文件夹", null, (s, e) => Process.Start("explorer.exe", "\"" + Host.AppDir + "\""));
            settings.DropDownItems.Add("查看日志", null, (s, e) => OpenFile(Path.Combine(Host.AppDir, "logs", "sop.log")));
            settings.DropDownItems.Add(new ToolStripSeparator());
            var auto = new ToolStripMenuItem("开机自启") { Checked = Host.AutoStart };
            auto.Click += (s, e) => { Host.AutoStart = !Host.AutoStart; auto.Checked = Host.AutoStart; };
            settings.DropDownItems.Add(auto);
            settings.DropDownItems.Add("接管旧软件（Snipaste / Umi-OCR / AHK）…", null, (s, e) => Migration.Run(Reload));
            settings.DropDownItems.Add("恢复旧软件的开机启动", null, (s, e) => Migration.Restore());
            menu.Items.Add(settings);

            var pause = new ToolStripMenuItem("暂停所有热键") { Checked = Host.Hotkeys.Paused };
            pause.Click += (s, e) =>
            {
                Host.Hotkeys.Paused = !Host.Hotkeys.Paused;
                pause.Checked = Host.Hotkeys.Paused;
                tray.Text = Host.AppName + (Host.Hotkeys.Paused ? "（已暂停）" : "");
            };
            menu.Items.Add(pause);
            menu.Items.Add(new ToolStripMenuItem("退出", null, (s, e) => Quit()));

            var old = tray.ContextMenuStrip;
            tray.ContextMenuStrip = menu;
            if (old != null) old.Dispose();
        }

        static void OpenFile(string path)
        {
            try
            {
                if (!File.Exists(path)) File.WriteAllText(path, "");
                Process.Start("notepad.exe", "\"" + path + "\"");
            }
            catch (Exception ex) { Host.Notify("打不开文件", ex.Message); }
        }

        public void Reload()
        {
            ShutdownModules();
            Host.Hotkeys.UnregisterAll();
            Host.Config.Load();
            LoadModules();
            int failed = Host.Hotkeys.All.Count(x => !x.Ok && x.Error != "未设置");
            Host.Notify("已重新加载", failed == 0 ? "所有热键已就绪" : failed + " 个热键注册失败，点我查看", failed > 0 ? (Action)HotkeyListForm.ShowList : null);
        }

        void ShutdownModules()
        {
            foreach (var m in modules)
            {
                try { m.Shutdown(); } catch (Exception ex) { Host.Log("模块 " + m.Id + " 关闭出错: " + ex); }
            }
            modules.Clear();
        }

        void Quit()
        {
            ShutdownModules();
            Host.Hotkeys.Dispose();
            tray.Visible = false;
            tray.Dispose();
            ExitThread();
        }
    }
}
