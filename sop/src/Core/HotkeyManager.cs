using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

namespace MySOP.Core
{
    public class HotkeyInfo
    {
        public string Module, Combo, Description;
        public bool Ok;
        public string Error;
    }

    /// <summary>
    /// 全局热键 + 进程间命令（WM_COPYDATA）都由这个隐藏窗口接收。
    /// 其它程序/脚本可以运行 `SOP.exe 命令名` 来触发本程序里的任意命令。
    /// </summary>
    public class HotkeyManager : NativeWindow, IDisposable
    {
        public const string WindowTitle = "MySOP_IPC_Window";
        readonly Dictionary<int, Action> actions = new Dictionary<int, Action>();
        public readonly List<HotkeyInfo> All = new List<HotkeyInfo>();
        public event Action<string> CommandReceived;
        int nextId = 1;
        bool paused;

        public HotkeyManager()
        {
            CreateHandle(new CreateParams { Caption = WindowTitle });
        }

        public HotkeyInfo Register(string module, string combo, string description, Action action)
        {
            var info = new HotkeyInfo { Module = module, Combo = combo, Description = description };
            All.Add(info);
            if (string.IsNullOrWhiteSpace(combo)) { info.Error = "未设置"; return info; }
            uint mods, vk;
            if (!TryParse(combo, out mods, out vk)) { info.Error = "无法解析"; return info; }
            int id = nextId++;
            if (Native.RegisterHotKey(Handle, id, mods | Native.MOD_NOREPEAT, vk))
            {
                info.Ok = true;
                actions[id] = action;
            }
            else
            {
                info.Error = "被其它程序占用（错误 " + Marshal.GetLastWin32Error() + "）";
            }
            return info;
        }

        public void UnregisterAll()
        {
            foreach (var id in actions.Keys) Native.UnregisterHotKey(Handle, id);
            actions.Clear();
            All.Clear();
        }

        public bool Paused
        {
            get { return paused; }
            set { paused = value; }
        }

        public static bool TryParse(string combo, out uint mods, out uint vk)
        {
            mods = 0; vk = 0;
            foreach (var raw in combo.Split('+'))
            {
                string p = raw.Trim().ToLowerInvariant();
                if (p.Length == 0) continue;
                switch (p)
                {
                    case "ctrl": case "control": mods |= Native.MOD_CONTROL; continue;
                    case "alt": mods |= Native.MOD_ALT; continue;
                    case "shift": mods |= Native.MOD_SHIFT; continue;
                    case "win": case "windows": mods |= Native.MOD_WIN; continue;
                }
                if (p.Length == 1 && char.IsDigit(p[0])) { vk = (uint)Keys.D0 + (uint)(p[0] - '0'); continue; }
                Keys k;
                if (p == "printscreen" || p == "prtsc") k = Keys.PrintScreen;
                else if (!Enum.TryParse(p, true, out k)) return false;
                vk = (uint)k;
            }
            return vk != 0;
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == Native.WM_HOTKEY)
            {
                Action a;
                if (!paused && actions.TryGetValue(m.WParam.ToInt32(), out a)) SafeRun(a);
                return;
            }
            if (m.Msg == Native.WM_COPYDATA)
            {
                var cds = (Native.COPYDATASTRUCT)Marshal.PtrToStructure(m.LParam, typeof(Native.COPYDATASTRUCT));
                string cmd = Marshal.PtrToStringUni(cds.lpData, cds.cbData / 2);
                var h = CommandReceived;
                if (h != null) Host.Ui.BeginInvoke(new Action(() => h(cmd)));
                m.Result = new IntPtr(1);
                return;
            }
            base.WndProc(ref m);
        }

        static void SafeRun(Action a)
        {
            try { a(); }
            catch (Exception ex) { Host.Log("热键动作出错: " + ex); }
        }

        /// <summary>把命令发给已在运行的实例。返回是否找到实例。</summary>
        public static bool SendToRunning(string cmd) { return SendTo(WindowTitle, cmd); }

        public static bool SendTo(string windowTitle, string cmd)
        {
            IntPtr hwnd = Native.FindWindow(null, windowTitle);
            if (hwnd == IntPtr.Zero) return false;
            byte[] bytes = Encoding.Unicode.GetBytes(cmd);
            IntPtr mem = Marshal.AllocHGlobal(bytes.Length + 2);
            try
            {
                Marshal.Copy(bytes, 0, mem, bytes.Length);
                var cds = new Native.COPYDATASTRUCT { dwData = IntPtr.Zero, cbData = bytes.Length, lpData = mem };
                Native.SendMessage(hwnd, Native.WM_COPYDATA, IntPtr.Zero, ref cds);
            }
            finally { Marshal.FreeHGlobal(mem); }
            return true;
        }

        public void Dispose()
        {
            UnregisterAll();
            DestroyHandle();
        }
    }
}
