using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

namespace MySOP.Core
{
    /// <summary>
    /// config.json 读写。每个模块拥有一个同名的 section；模块在 Init 时通过
    /// Section(...).Default(...) 声明默认值，缺失的键会自动补进文件，
    /// 所以新加模块后配置文件会自己“长出”对应的配置项。
    /// </summary>
    public class Config
    {
        public readonly string Path;
        public Dictionary<string, object> Root;
        bool dirty;

        public Config(string path)
        {
            Path = path;
            Load();
        }

        public void Load()
        {
            Root = new Dictionary<string, object>();
            if (File.Exists(Path))
            {
                var obj = new JavaScriptSerializer().DeserializeObject(File.ReadAllText(Path, Encoding.UTF8));
                var d = obj as Dictionary<string, object>;
                if (d != null) Root = d;
            }
        }

        public ConfigSection Section(string name)
        {
            object o;
            if (!Root.TryGetValue(name, out o) || !(o is Dictionary<string, object>))
            {
                o = new Dictionary<string, object>();
                Root[name] = o;
                dirty = true;
            }
            return new ConfigSection(this, (Dictionary<string, object>)o);
        }

        /// <summary>取顶层数组（如 workflows），不存在则写入默认值。</summary>
        public object[] Array(string name, object[] defaults)
        {
            object o;
            if (!Root.TryGetValue(name, out o) || !(o is object[]))
            {
                Root[name] = defaults;
                dirty = true;
                return defaults;
            }
            return (object[])o;
        }

        internal void MarkDirty() { dirty = true; }

        public void SaveIfDirty() { if (dirty) Save(); }

        public void Save()
        {
            var sb = new StringBuilder();
            WriteJson(sb, Root, 0);
            File.WriteAllText(Path, sb.ToString(), new UTF8Encoding(false));
            dirty = false;
        }

        // ---------- 简易带缩进的 JSON 输出 ----------
        static void WriteJson(StringBuilder sb, object v, int indent)
        {
            string pad = new string(' ', indent * 2), pad2 = new string(' ', (indent + 1) * 2);
            if (v == null) sb.Append("null");
            else if (v is string) WriteString(sb, (string)v);
            else if (v is bool) sb.Append((bool)v ? "true" : "false");
            else if (v is IDictionary)
            {
                var d = (IDictionary)v;
                if (d.Count == 0) { sb.Append("{}"); return; }
                sb.Append("{\n");
                int i = 0;
                foreach (DictionaryEntry e in d)
                {
                    sb.Append(pad2); WriteString(sb, e.Key.ToString()); sb.Append(": ");
                    WriteJson(sb, e.Value, indent + 1);
                    if (++i < d.Count) sb.Append(',');
                    sb.Append('\n');
                }
                sb.Append(pad).Append('}');
            }
            else if (v is IEnumerable)
            {
                var list = new List<object>();
                foreach (var x in (IEnumerable)v) list.Add(x);
                if (list.Count == 0) { sb.Append("[]"); return; }
                sb.Append("[\n");
                for (int i = 0; i < list.Count; i++)
                {
                    sb.Append(pad2); WriteJson(sb, list[i], indent + 1);
                    if (i < list.Count - 1) sb.Append(',');
                    sb.Append('\n');
                }
                sb.Append(pad).Append(']');
            }
            else if (v is IFormattable) sb.Append(((IFormattable)v).ToString(null, CultureInfo.InvariantCulture));
            else WriteString(sb, v.ToString());
        }

        static void WriteString(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.AppendFormat("\\u{0:x4}", (int)c); else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }

        /// <summary>把字符串编码为纯 ASCII 的 JSON 字符串（给 OCR 引擎用）。</summary>
        public static string ToAsciiJsonString(string s)
        {
            var sb = new StringBuilder("\"");
            foreach (char c in s)
            {
                if (c == '"') sb.Append("\\\"");
                else if (c == '\\') sb.Append("\\\\");
                else if (c < 0x20 || c > 0x7e) sb.AppendFormat("\\u{0:x4}", (int)c);
                else sb.Append(c);
            }
            return sb.Append('"').ToString();
        }
    }

    public class ConfigSection
    {
        readonly Config owner;
        public readonly Dictionary<string, object> Data;

        public ConfigSection(Config owner, Dictionary<string, object> data) { this.owner = owner; Data = data; }

        /// <summary>声明默认值：键不存在时写入。</summary>
        public ConfigSection Default(string key, object value)
        {
            if (!Data.ContainsKey(key)) { Data[key] = value; owner.MarkDirty(); }
            return this;
        }

        public string Str(string key, string def = "")
        {
            object o;
            return Data.TryGetValue(key, out o) && o != null ? Convert.ToString(o, CultureInfo.InvariantCulture) : def;
        }

        public bool Bool(string key, bool def = false)
        {
            object o;
            if (!Data.TryGetValue(key, out o) || o == null) return def;
            if (o is bool) return (bool)o;
            bool b;
            return bool.TryParse(o.ToString(), out b) ? b : def;
        }

        public int Int(string key, int def = 0)
        {
            object o;
            if (!Data.TryGetValue(key, out o) || o == null) return def;
            try { return Convert.ToInt32(o, CultureInfo.InvariantCulture); } catch { return def; }
        }

        public void Set(string key, object value) { Data[key] = value; owner.MarkDirty(); }
    }
}
