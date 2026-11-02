using System;
using System.Collections.Generic;
using System.Drawing;
using System.Linq;
using System.Text;

namespace MySOP.Ocr
{
    /// <summary>
    /// 文本排版（对应 Umi-OCR 的“排版解析方案”，名字保持一致）：
    ///   multi_none  多栏-无换行（你原来的设置）   multi_line  多栏-总是换行   multi_para  多栏-按自然段换行
    ///   single_none 单栏-无换行   single_line 单栏-总是换行   single_para 单栏-按自然段换行
    ///   none        不处理（引擎原始顺序，每块一行）
    /// </summary>
    public static class TextLayout
    {
        public static readonly string[][] Modes =
        {
            new[] { "multi_none", "多栏 - 无换行" }, new[] { "multi_line", "多栏 - 总是换行" }, new[] { "multi_para", "多栏 - 按自然段换行" },
            new[] { "single_none", "单栏 - 无换行" }, new[] { "single_line", "单栏 - 总是换行" }, new[] { "single_para", "单栏 - 按自然段换行" },
            new[] { "none", "不处理" },
        };

        class Line
        {
            public List<OcrBlock> Blocks = new List<OcrBlock>();
            public Rectangle Box;
            public string Text;
        }

        public static string Format(List<OcrBlock> blocks, string mode)
        {
            if (blocks == null || blocks.Count == 0) return "";
            if (mode == "none") return string.Join("\n", blocks.Select(b => b.Text));
            bool multi = mode.StartsWith("multi");
            string join = mode.EndsWith("_none") ? "none" : mode.EndsWith("_line") ? "line" : "para";

            var leaves = new List<List<OcrBlock>>();
            Segment(blocks, multi, leaves, 0);

            var sb = new StringBuilder();
            foreach (var leaf in leaves)
            {
                var lines = BuildLines(leaf);
                int left = lines.Min(l => l.Box.Left), right = lines.Max(l => l.Box.Right);
                for (int i = 0; i < lines.Count; i++)
                {
                    var l = lines[i];
                    if (sb.Length > 0)
                    {
                        bool newline;
                        if (join == "none") newline = false;
                        else if (join == "line") newline = true;
                        else if (i == 0) newline = true; // 新的栏/段块
                        else
                        {
                            var prev = lines[i - 1];
                            int h = Math.Max(1, (prev.Box.Height + l.Box.Height) / 2);
                            bool prevShort = prev.Box.Right < right - h * 1.5;
                            bool indented = l.Box.Left > left + h * 0.9;
                            newline = prevShort || indented;
                        }
                        if (newline) sb.Append('\n');
                        else AppendSeparator(sb, l.Text);
                    }
                    sb.Append(l.Text);
                }
            }
            return sb.ToString();
        }

        static void AppendSeparator(StringBuilder sb, string next)
        {
            if (sb.Length == 0 || string.IsNullOrEmpty(next)) return;
            char a = sb[sb.Length - 1], b = next[0];
            if (a == '\n' || char.IsWhiteSpace(a) || char.IsWhiteSpace(b)) return;
            if (IsCjk(a) || IsCjk(b)) return; // 中文之间不加空格
            sb.Append(' ');
        }

        static bool IsCjk(char c) { return c >= 0x2E80 && c <= 0xFFEF && !(c >= 0xFF10 && c <= 0xFF19); }

        static int MedianHeight(List<OcrBlock> blocks)
        {
            var hs = blocks.Select(b => b.Box.Height).OrderBy(h => h).ToList();
            return Math.Max(4, hs[hs.Count / 2]);
        }

        /// <summary>递归 XY 切分：先按竖直空白分栏，再按较大的水平空白分块。</summary>
        static void Segment(List<OcrBlock> blocks, bool allowColumns, List<List<OcrBlock>> leaves, int depth)
        {
            if (blocks.Count <= 1 || depth > 12) { leaves.Add(blocks); return; }
            int mh = MedianHeight(blocks);
            if (allowColumns)
            {
                var cols = SplitByGaps(blocks, b => b.Box.Left, b => b.Box.Right, Math.Max(mh, 10));
                if (cols.Count > 1) { foreach (var c in cols) Segment(c, true, leaves, depth + 1); return; }
                var bands = SplitByGaps(blocks, b => b.Box.Top, b => b.Box.Bottom, Math.Max((int)(mh * 0.8), 4));
                if (bands.Count > 1)
                {
                    // 只有当某个块内部还能继续分栏时才拆开；否则保持为一个整体，便于判断段落
                    bool anyCols = bands.Any(band => band.Count > 1 && SplitByGaps(band, b => b.Box.Left, b => b.Box.Right, Math.Max(MedianHeight(band), 10)).Count > 1);
                    if (anyCols) { foreach (var band in bands) Segment(band, true, leaves, depth + 1); return; }
                }
            }
            leaves.Add(blocks);
        }

        static List<List<OcrBlock>> SplitByGaps(List<OcrBlock> blocks, Func<OcrBlock, int> start, Func<OcrBlock, int> end, int minGap)
        {
            var sorted = blocks.OrderBy(start).ToList();
            var groups = new List<List<OcrBlock>>();
            var cur = new List<OcrBlock>();
            int curEnd = int.MinValue;
            foreach (var b in sorted)
            {
                if (cur.Count > 0 && start(b) - curEnd > minGap) { groups.Add(cur); cur = new List<OcrBlock>(); curEnd = int.MinValue; }
                cur.Add(b);
                curEnd = Math.Max(curEnd, end(b));
            }
            if (cur.Count > 0) groups.Add(cur);
            return groups;
        }

        static List<Line> BuildLines(List<OcrBlock> blocks)
        {
            var lines = new List<Line>();
            foreach (var b in blocks.OrderBy(b => b.Box.Top + b.Box.Height / 2))
            {
                Line target = null;
                foreach (var l in lines)
                {
                    int overlap = Math.Min(l.Box.Bottom, b.Box.Bottom) - Math.Max(l.Box.Top, b.Box.Top);
                    if (overlap > Math.Min(l.Box.Height, b.Box.Height) * 0.5) { target = l; break; }
                }
                if (target == null) { target = new Line { Box = b.Box }; lines.Add(target); }
                else target.Box = Rectangle.Union(target.Box, b.Box);
                target.Blocks.Add(b);
            }
            foreach (var l in lines)
            {
                var sb = new StringBuilder();
                OcrBlock prev = null;
                foreach (var b in l.Blocks.OrderBy(x => x.Box.Left))
                {
                    if (prev != null)
                    {
                        int gap = b.Box.Left - prev.Box.Right;
                        if (gap > l.Box.Height * 0.6 && !sb.ToString().EndsWith(" ")) sb.Append(' ');
                        else AppendSeparator(sb, b.Text);
                    }
                    sb.Append(b.Text);
                    prev = b;
                }
                l.Text = sb.ToString();
            }
            return lines.OrderBy(l => l.Box.Top).ToList();
        }
    }
}
