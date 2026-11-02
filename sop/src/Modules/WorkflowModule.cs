using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using MySOP.Core;

namespace MySOP.Modules
{
    /// <summary>
    /// 自定义工作流：不写代码，在 config.json 的 "workflows" 里加一项就是一个新功能。
    ///   type = open     打开文件 / 文件夹 / 网址 / 程序（target, args）
    ///   type = type     直接输入一段文字（text）
    ///   type = paste    通过剪贴板粘贴一段文字（text，适合很长的内容）
    ///   type = command  执行内置命令，可多个，用逗号分隔（target，例如 "capture" 或 "ocr-clipboard"）
    /// 文字里可用 {date} {time} {datetime} {clipboard}。
    /// </summary>
    public class WorkflowModule : IModule
    {
        public string Id { get { return "workflows"; } }
        public string Name { get { return "自定义工作流"; } }

        public void Init(ModuleContext ctx)
        {
            var items = Host.Config.Array("workflows", new object[]
            {
                Example("示例：打开下载文件夹", "open", "target", Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads")),
                Example("示例：输入今天日期", "type", "text", "{date}"),
                Example("示例：截图识字（调用内置命令）", "command", "target", "ocr"),
            });

            var sub = new ToolStripMenuItem("工作流");
            foreach (var o in items)
            {
                var d = o as Dictionary<string, object>;
                if (d == null) continue;
                var wf = new ConfigSection(Host.Config, d);
                string name = wf.Str("name", "未命名");
                if (wf.Bool("disabled")) continue;
                Action run = () => Run(wf);
                string cmdName = "wf:" + name;
                ctx.Command(cmdName, name, run);
                var item = new ToolStripMenuItem(name, null, (s, e) => run());
                string hk = wf.Str("hotkey");
                if (hk.Length > 0)
                {
                    var info = Host.Hotkeys.Register(Name, hk, name, run);
                    item.ShortcutKeyDisplayString = hk + (info.Ok ? "" : "（冲突）");
                }
                sub.DropDownItems.Add(item);
            }
            if (sub.DropDownItems.Count > 0) sub.DropDownItems.Add(new ToolStripSeparator());
            sub.DropDownItems.Add("编辑工作流（config.json）…", null, (s, e) => Process.Start("notepad.exe", "\"" + Host.Config.Path + "\""));
            sub.DropDownItems.Add("重新加载", null, (s, e) => Host.RunCommand("reload"));
            ctx.MenuItem(sub);
        }

        static Dictionary<string, object> Example(string name, string type, string key, string value)
        {
            return new Dictionary<string, object> { { "name", name }, { "hotkey", "" }, { "type", type }, { key, value } };
        }

        static void Run(ConfigSection wf)
        {
            string type = wf.Str("type").ToLowerInvariant();
            switch (type)
            {
                case "open":
                case "run":
                    var psi = new ProcessStartInfo(Expand(wf.Str("target")), Expand(wf.Str("args"))) { UseShellExecute = true };
                    string wd = wf.Str("workingDir");
                    if (wd.Length > 0) psi.WorkingDirectory = wd;
                    Process.Start(psi);
                    break;
                case "type":
                    string text = Expand(wf.Str("text"));
                    Task.Run(() => { WaitModifiersReleased(); SendText(text); });
                    break;
                case "paste":
                    Paste(Expand(wf.Str("text")));
                    break;
                case "command":
                    foreach (var c in wf.Str("target").Split(',').Select(s => s.Trim()).Where(s => s.Length > 0))
                        Host.RunCommand(c);
                    break;
                default:
                    Host.Notify("未知的工作流类型", type);
                    break;
            }
        }

        static string Expand(string s)
        {
            if (string.IsNullOrEmpty(s) || s.IndexOf('{') < 0) return s ?? "";
            var now = DateTime.Now;
            s = s.Replace("{date}", now.ToString("yyyy-MM-dd")).Replace("{time}", now.ToString("HH:mm")).Replace("{datetime}", now.ToString("yyyy-MM-dd HH:mm"));
            if (s.Contains("{clipboard}")) s = s.Replace("{clipboard}", Clipboard.ContainsText() ? Clipboard.GetText() : "");
            return Environment.ExpandEnvironmentVariables(s);
        }

        static void WaitModifiersReleased()
        {
            int[] mods = { 0x10, 0x11, 0x12, 0x5B, 0x5C };
            for (int i = 0; i < 100 && mods.Any(m => (Native.GetAsyncKeyState(m) & 0x8000) != 0); i++) Thread.Sleep(20);
        }

        public static void SendText(string text)
        {
            var inputs = new List<Native.INPUT>();
            foreach (char ch in text.Replace("\r\n", "\n"))
            {
                if (ch == '\n') { Key(inputs, 0x0D, 0); Key(inputs, 0x0D, Native.KEYEVENTF_KEYUP); continue; }
                inputs.Add(new Native.INPUT { type = Native.INPUT_KEYBOARD, U = new Native.InputUnion { ki = new Native.KEYBDINPUT { wScan = ch, dwFlags = Native.KEYEVENTF_UNICODE } } });
                inputs.Add(new Native.INPUT { type = Native.INPUT_KEYBOARD, U = new Native.InputUnion { ki = new Native.KEYBDINPUT { wScan = ch, dwFlags = Native.KEYEVENTF_UNICODE | Native.KEYEVENTF_KEYUP } } });
            }
            Native.SendInput((uint)inputs.Count, inputs.ToArray(), Marshal.SizeOf(typeof(Native.INPUT)));
        }

        static void Key(List<Native.INPUT> inputs, ushort vk, uint flags)
        {
            inputs.Add(new Native.INPUT { type = Native.INPUT_KEYBOARD, U = new Native.InputUnion { ki = new Native.KEYBDINPUT { wVk = vk, dwFlags = flags } } });
        }

        static void Paste(string text)
        {
            string old = null;
            try { if (Clipboard.ContainsText()) old = Clipboard.GetText(); } catch { }
            Clipboard.SetText(text);
            Task.Run(() =>
            {
                WaitModifiersReleased();
                var inputs = new List<Native.INPUT>();
                Key(inputs, 0x11, 0); Key(inputs, 0x56, 0); Key(inputs, 0x56, Native.KEYEVENTF_KEYUP); Key(inputs, 0x11, Native.KEYEVENTF_KEYUP);
                Native.SendInput((uint)inputs.Count, inputs.ToArray(), Marshal.SizeOf(typeof(Native.INPUT)));
                Thread.Sleep(600);
                if (old != null) Host.OnUi(() => { try { Clipboard.SetText(old); } catch { } });
            });
        }

        public void Shutdown() { }
    }
}
