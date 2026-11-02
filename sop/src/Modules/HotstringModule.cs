using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;
using MySOP.Core;

namespace MySOP.Modules
{
    public class Hotstring
    {
        public string Abbr, Replacement;
        public bool Immediate;   // *  不需要结束符
        public bool InsideWord;  // ?  在单词中间也触发
        public bool CaseSensitive; // C
        public bool OmitEnding;  // O  不输出结束符
    }

    /// <summary>
    /// 文本替换（替代 AHK 的 Reference.ahk）。直接读 AHK 写法的 hotstrings.txt：
    ///   ::r1::[1]        输入 r1 再按 空格/回车/标点 → [1]
    ///   :*:;sj::...      带 * 号：输完立即替换
    /// 行为与 AHK 默认一致：缩写前面必须是开头或结束符；替换后保留你按的结束符。
    /// </summary>
    public class HotstringModule : IModule
    {
        public string Id { get { return "hotstrings"; } }
        public string Name { get { return "快捷短语"; } }

        const string EndChars = "-()[]{}':;\"/\\,.?!\n\t ";

        List<Hotstring> list = new List<Hotstring>();
        string file;
        bool acceptInjected, paused;
        IntPtr kbHook, mouseHook;
        Native.HookProc kbProc, mouseProc; // 保持引用，防止被 GC
        readonly StringBuilder buffer = new StringBuilder();
        IntPtr lastForeground;
        FileSystemWatcher watcher;
        Timer reloadTimer;

        public void Init(ModuleContext ctx)
        {
            ctx.Config.Default("file", "hotstrings.txt").Default("acceptInjected", false);
            file = ctx.Config.Str("file");
            if (!Path.IsPathRooted(file)) file = Path.Combine(Host.AppDir, file);
            acceptInjected = ctx.Config.Bool("acceptInjected");
            if (!File.Exists(file)) File.WriteAllText(file, DefaultFile, new UTF8Encoding(true));
            Load(false);

            kbProc = KeyboardProc;
            mouseProc = MouseProc;
            IntPtr mod = Native.GetModuleHandle(null);
            kbHook = Native.SetWindowsHookEx(Native.WH_KEYBOARD_LL, kbProc, mod, 0);
            mouseHook = Native.SetWindowsHookEx(Native.WH_MOUSE_LL, mouseProc, mod, 0);
            if (kbHook == IntPtr.Zero) Host.Notify("快捷短语启动失败", "键盘钩子安装失败：" + Marshal.GetLastWin32Error());

            watcher = new FileSystemWatcher(Path.GetDirectoryName(file), Path.GetFileName(file)) { NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.Size };
            watcher.Changed += (s, e) => Host.OnUi(() => { reloadTimer.Stop(); reloadTimer.Start(); });
            watcher.EnableRaisingEvents = true;
            reloadTimer = new Timer { Interval = 400 };
            reloadTimer.Tick += (s, e) => { reloadTimer.Stop(); Load(true); };

            ctx.Command("hotstrings-edit", "编辑快捷短语", Edit);
            ctx.Menu("编辑快捷短语…", Edit);
            var pause = ctx.Menu("暂停快捷短语", null);
            pause.Click += (s, e) => { paused = !paused; pause.Checked = paused; buffer.Length = 0; };
        }

        void Edit() { Process.Start("notepad.exe", "\"" + file + "\""); }

        void Load(bool notify)
        {
            try
            {
                string[] warnings;
                list = Parse(File.ReadAllLines(file, Encoding.UTF8), out warnings);
                // 长的缩写优先匹配
                list = list.OrderByDescending(h => h.Abbr.Length).ToList();
                if (notify) Host.Notify("快捷短语已更新", list.Count + " 条" + (warnings.Length > 0 ? "\n" + string.Join("\n", warnings) : ""));
                else if (warnings.Length > 0) Host.Log("hotstrings 警告:\n" + string.Join("\n", warnings));
            }
            catch (IOException) { /* 编辑器还在写，下次再读 */ }
        }

        public static List<Hotstring> Parse(IEnumerable<string> lines, out string[] warnings)
        {
            var result = new List<Hotstring>();
            var warn = new List<string>();
            int n = 0;
            foreach (var raw in lines)
            {
                n++;
                string line = raw.Trim();
                if (line.Length == 0 || line.StartsWith(";") || line.StartsWith("#")) continue;
                if (!line.StartsWith(":")) { warn.Add("第 " + n + " 行不是短语格式，已忽略"); continue; }
                int optEnd = line.IndexOf(':', 1);
                if (optEnd < 0) continue;
                string opts = line.Substring(1, optEnd - 1).ToUpperInvariant();
                int sep = line.IndexOf("::", optEnd + 1, StringComparison.Ordinal);
                if (sep < 0) { warn.Add("第 " + n + " 行缺少 ::"); continue; }
                string abbr = line.Substring(optEnd + 1, sep - optEnd - 1);
                string rep = line.Substring(sep + 2);
                if (opts.Contains("X")) { warn.Add("第 " + n + " 行是 AHK 代码短语（X），不支持"); continue; }
                if (abbr.Length == 0) continue;
                result.Add(new Hotstring
                {
                    Abbr = abbr,
                    Replacement = Unescape(StripComment(rep)).TrimEnd(' ', '\t'),
                    Immediate = opts.Contains("*") && !opts.Contains("*0"),
                    InsideWord = opts.Contains("?"),
                    CaseSensitive = opts.Contains("C") && !opts.Contains("C0") && !opts.Contains("C1"),
                    OmitEnding = opts.Contains("O") && !opts.Contains("O0"),
                });
            }
            warnings = warn.ToArray();
            return result;
        }

        static string StripComment(string s)
        {
            // AHK：空格/Tab 后的 ; 是注释（`; 表示真正的分号）
            for (int i = 1; i < s.Length; i++)
                if (s[i] == ';' && (s[i - 1] == ' ' || s[i - 1] == '\t') && (i < 2 || s[i - 2] != '`')) return s.Substring(0, i - 1);
            return s;
        }

        static string Unescape(string s)
        {
            s = s.Replace("{Enter}", "\n").Replace("{enter}", "\n").Replace("{Tab}", "\t").Replace("{tab}", "\t").Replace("{Space}", " ").Replace("{space}", " ");
            var sb = new StringBuilder();
            for (int i = 0; i < s.Length; i++)
            {
                if (s[i] == '`' && i + 1 < s.Length)
                {
                    char c = s[++i];
                    sb.Append(c == 'n' ? '\n' : c == 't' ? '\t' : c == 'r' ? '\r' : c == 's' ? ' ' : c);
                }
                else sb.Append(s[i]);
            }
            return sb.ToString().Replace("\r\n", "\n").Replace("\r", "\n");
        }

        // ---------- 钩子 ----------
        IntPtr MouseProc(int nCode, IntPtr wParam, IntPtr lParam)
        {
            if (nCode >= 0)
            {
                int m = wParam.ToInt32();
                if (m == Native.WM_LBUTTONDOWN || m == Native.WM_RBUTTONDOWN || m == Native.WM_MBUTTONDOWN) buffer.Length = 0;
            }
            return Native.CallNextHookEx(mouseHook, nCode, wParam, lParam);
        }

        IntPtr KeyboardProc(int nCode, IntPtr wParam, IntPtr lParam)
        {
            if (nCode >= 0)
            {
                int msg = wParam.ToInt32();
                if (msg == Native.WM_KEYDOWN || msg == Native.WM_SYSKEYDOWN)
                {
                    var k = (Native.KBDLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(Native.KBDLLHOOKSTRUCT));
                    if ((k.flags & Native.LLKHF_INJECTED) == 0 || acceptInjected)
                    {
                        try { if (OnKeyDown(k)) return new IntPtr(1); }
                        catch (Exception ex) { Host.Log("hotstring hook: " + ex.Message); buffer.Length = 0; }
                    }
                }
            }
            return Native.CallNextHookEx(kbHook, nCode, wParam, lParam);
        }

        static bool Down(int vk) { return (Native.GetAsyncKeyState(vk) & 0x8000) != 0; }

        /// <summary>返回 true 表示吞掉这次按键。</summary>
        bool OnKeyDown(Native.KBDLLHOOKSTRUCT k)
        {
            if (paused || Host.Hotkeys.Paused || list.Count == 0) return false;
            uint vk = k.vkCode;
            if ((vk >= 0x10 && vk <= 0x12) || (vk >= 0xA0 && vk <= 0xA5) || vk == 0x5B || vk == 0x5C || vk == 0x14) return false; // 修饰键本身

            var fg = Native.GetForegroundWindow();
            if (fg != lastForeground) { buffer.Length = 0; lastForeground = fg; }

            if (Down(0x11) || Down(0x12) || Down(0x5B) || Down(0x5C)) { buffer.Length = 0; return false; } // Ctrl/Alt/Win 组合
            if (vk == 0x08) { if (buffer.Length > 0) buffer.Length--; return false; }

            char c;
            if (vk == 0x0D) c = '\n';
            else if (vk == 0x09) c = '\t';
            else
            {
                var state = new byte[256];
                if (Down(0x10)) state[0x10] = 0x80;
                if ((Native.GetKeyState(0x14) & 1) != 0) state[0x14] = 1;
                uint tid; uint pid;
                tid = Native.GetWindowThreadProcessId(fg, out pid);
                var sb = new StringBuilder(4);
                int r = Native.ToUnicodeEx(vk, k.scanCode, state, sb, sb.Capacity, 4, Native.GetKeyboardLayout(tid));
                if (r != 1) { buffer.Length = 0; return false; } // 方向键、功能键、死键等：清空
                c = sb[0];
                if (char.IsControl(c)) { buffer.Length = 0; return false; }
            }

            int backspaces; string text; char? ending;
            if (!ProcessChar(c, out backspaces, out text, out ending)) return false;
            Host.Ui.BeginInvoke(new Action(() => Replace(backspaces, text, ending)));
            return true;
        }

        /// <summary>
        /// 处理一个输入字符。返回 true = 命中短语：吞掉这个字符，
        /// 然后退格 backspaces 次、输入 text、再输入 ending（可能为空）。
        /// </summary>
        public bool ProcessChar(char c, out int backspaces, out string text, out char? ending)
        {
            backspaces = 0; text = null; ending = null;
            if (EndChars.IndexOf(c) >= 0)
            {
                var h = Match(false);
                if (h != null)
                {
                    buffer.Length = 0;
                    backspaces = h.Abbr.Length; text = h.Replacement; ending = h.OmitEnding ? (char?)null : c;
                    return true;
                }
                buffer.Append(c);
            }
            else
            {
                buffer.Append(c);
                var h = Match(true);
                if (h != null)
                {
                    buffer.Length = 0;
                    backspaces = h.Abbr.Length - 1; text = h.Replacement;
                    return true;
                }
            }
            if (buffer.Length > 100) buffer.Remove(0, buffer.Length - 60);
            return false;
        }

        /// <summary>自检用：模拟在空白文档里逐字输入，返回最终文本。</summary>
        public static string Simulate(List<Hotstring> hs, string typed)
        {
            var m = new HotstringModule { list = hs.OrderByDescending(h => h.Abbr.Length).ToList() };
            var doc = new StringBuilder();
            foreach (char c in typed)
            {
                if (c == '\b') { if (doc.Length > 0) doc.Length--; if (m.buffer.Length > 0) m.buffer.Length--; continue; }
                int bs; string text; char? end;
                if (m.ProcessChar(c, out bs, out text, out end))
                {
                    doc.Length = Math.Max(0, doc.Length - bs);
                    doc.Append(text);
                    if (end.HasValue) doc.Append(end.Value);
                }
                else doc.Append(c);
            }
            return doc.ToString();
        }

        Hotstring Match(bool immediate)
        {
            string buf = buffer.ToString();
            foreach (var h in list)
            {
                if (h.Immediate != immediate || buf.Length < h.Abbr.Length) continue;
                if (!buf.EndsWith(h.Abbr, h.CaseSensitive ? StringComparison.Ordinal : StringComparison.OrdinalIgnoreCase)) continue;
                int before = buf.Length - h.Abbr.Length - 1;
                if (!h.InsideWord && before >= 0 && EndChars.IndexOf(buf[before]) < 0) continue;
                return h;
            }
            return null;
        }

        static void Replace(int backspaces, string text, char? ending)
        {
            var inputs = new List<Native.INPUT>();
            for (int i = 0; i < backspaces; i++) AddVk(inputs, 0x08);
            foreach (char ch in text) AddChar(inputs, ch);
            if (ending.HasValue) AddChar(inputs, ending.Value);
            Native.SendInput((uint)inputs.Count, inputs.ToArray(), Marshal.SizeOf(typeof(Native.INPUT)));
        }

        static void AddChar(List<Native.INPUT> inputs, char ch)
        {
            if (ch == '\n') { AddVk(inputs, 0x0D); return; }
            if (ch == '\t') { AddVk(inputs, 0x09); return; }
            foreach (uint flags in new[] { Native.KEYEVENTF_UNICODE, Native.KEYEVENTF_UNICODE | Native.KEYEVENTF_KEYUP })
                inputs.Add(new Native.INPUT { type = Native.INPUT_KEYBOARD, U = new Native.InputUnion { ki = new Native.KEYBDINPUT { wScan = ch, dwFlags = flags } } });
        }

        static void AddVk(List<Native.INPUT> inputs, ushort vk)
        {
            foreach (uint flags in new[] { 0u, Native.KEYEVENTF_KEYUP })
                inputs.Add(new Native.INPUT { type = Native.INPUT_KEYBOARD, U = new Native.InputUnion { ki = new Native.KEYBDINPUT { wVk = vk, dwFlags = flags } } });
        }

        public void Shutdown()
        {
            if (kbHook != IntPtr.Zero) Native.UnhookWindowsHookEx(kbHook);
            if (mouseHook != IntPtr.Zero) Native.UnhookWindowsHookEx(mouseHook);
            kbHook = mouseHook = IntPtr.Zero;
            if (watcher != null) watcher.Dispose();
            if (reloadTimer != null) reloadTimer.Dispose();
        }

        const string DefaultFile =
            "; 快捷短语（AHK 写法，保存后自动生效）\r\n" +
            "; ::缩写::替换内容      输入缩写后按 空格/回车/Tab/标点 触发\r\n" +
            "; :*:缩写::替换内容     输完缩写立即触发\r\n" +
            "; :?:缩写::替换内容     在单词中间也触发\r\n" +
            "; 换行写 `n 或 {Enter}\r\n\r\n";
    }
}
