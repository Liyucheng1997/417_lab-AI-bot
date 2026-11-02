using System;
using System.Collections.Generic;
using System.Windows.Forms;

namespace MySOP.Core
{
    /// <summary>
    /// 一个“工作流模块”。新增功能 = 新写一个类实现这个接口，
    /// 然后在 Program.BuiltinModules 里加一行（或编译成 DLL 丢进 plugins 文件夹）。
    /// </summary>
    public interface IModule
    {
        /// <summary>英文 id，同时也是 config.json 里的 section 名。</summary>
        string Id { get; }
        /// <summary>显示名称。</summary>
        string Name { get; }
        void Init(ModuleContext ctx);
        void Shutdown();
    }

    public class CommandInfo
    {
        public string Name, Description, Module;
        public Action Run;
    }

    /// <summary>模块初始化时拿到的上下文：配置、热键、托盘菜单、命令。</summary>
    public class ModuleContext
    {
        readonly IModule module;
        internal readonly List<ToolStripItem> MenuItems = new List<ToolStripItem>();
        public readonly ConfigSection Config;

        internal ModuleContext(IModule module)
        {
            this.module = module;
            Config = Host.Config.Section(module.Id);
        }

        /// <summary>
        /// 注册热键：组合键读自配置 configKey（不存在则写入 defaultCombo）。
        /// </summary>
        public HotkeyInfo Hotkey(string configKey, string defaultCombo, string description, Action action)
        {
            Config.Default(configKey, defaultCombo);
            return Host.Hotkeys.Register(module.Name, Config.Str(configKey), description, action);
        }

        /// <summary>注册一个命令：可被托盘菜单、工作流、命令行 `SOP.exe 命令名` 调用。</summary>
        public void Command(string name, string description, Action run)
        {
            Host.Commands[name] = new CommandInfo { Name = name, Description = description, Module = module.Name, Run = run };
        }

        public ToolStripMenuItem Menu(string text, Action onClick)
        {
            var item = new ToolStripMenuItem(text);
            if (onClick != null) item.Click += (s, e) => onClick();
            MenuItems.Add(item);
            return item;
        }

        public void MenuItem(ToolStripItem item) { MenuItems.Add(item); }
    }
}
