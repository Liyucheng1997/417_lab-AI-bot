using System;
using System.Drawing;
using System.Runtime.InteropServices;
using MySOP.Core;

namespace MySOP.Record
{
    /// <summary>
    /// 用显卡截屏（DXGI Desktop Duplication）：4K 全屏一帧只要几毫秒，GDI 要 60 毫秒。
    /// 只支持完整落在一块显示器里的区域；用不了时返回 null，录屏引擎退回 GDI。
    /// COM 方法按 vtable 序号调用（序号取自 Windows SDK 头文件，含 IUnknown 的 3 个）。
    /// </summary>
    sealed class DesktopDuplication : IDisposable
    {
        IntPtr device, context, output1, dupl, staging;
        readonly Rectangle local;   // 区域在这块显示器里的坐标
        IntPtr frameInfo = Marshal.AllocHGlobal(64);
        bool hasContent;
        int lostCount;

        const int DXGI_ERROR_WAIT_TIMEOUT = unchecked((int)0x887A0027);
        const int DXGI_ERROR_ACCESS_LOST = unchecked((int)0x887A0026);
        static Guid IID_IDXGIFactory1 = new Guid("770aae78-f26f-4dba-a829-253c83d1b387");
        static Guid IID_IDXGIOutput1 = new Guid("00cddea8-939b-4b83-a340-a685226666cc");
        static Guid IID_ID3D11Texture2D = new Guid("6f15aaf2-d208-4e89-9ab4-489535d34f9c");

        public static DesktopDuplication TryCreate(Rectangle region)
        {
            try { return new DesktopDuplication(region); }
            catch (Exception ex) { Host.Log("显卡截屏不可用，改用 GDI: " + ex.Message); return null; }
        }

        DesktopDuplication(Rectangle region)
        {
            IntPtr factory;
            Check(CreateDXGIFactory1(ref IID_IDXGIFactory1, out factory));
            IntPtr adapter = IntPtr.Zero, output = IntPtr.Zero;
            try
            {
                // 找到包含这块区域的显示器（以及它所在的显卡）
                Rectangle bounds = Rectangle.Empty;
                for (int a = 0; output == IntPtr.Zero && Call<EnumDel>(factory, 12)(factory, a, out adapter) >= 0; a++)
                {
                    for (int o = 0; Call<EnumDel>(adapter, 7)(adapter, o, out output) >= 0; o++)
                    {
                        IntPtr desc = Marshal.AllocHGlobal(128);
                        try
                        {
                            Check(Call<PtrDel>(output, 7)(output, desc));
                            var r = Rectangle.FromLTRB(Marshal.ReadInt32(desc, 64), Marshal.ReadInt32(desc, 68), Marshal.ReadInt32(desc, 72), Marshal.ReadInt32(desc, 76));
                            int rotation = Marshal.ReadInt32(desc, 84);
                            if (r.Contains(region) && rotation <= 1) { bounds = r; break; }
                        }
                        finally { Marshal.FreeHGlobal(desc); }
                        Marshal.Release(output); output = IntPtr.Zero;
                    }
                    if (output == IntPtr.Zero) { Marshal.Release(adapter); adapter = IntPtr.Zero; }
                }
                if (output == IntPtr.Zero) throw new NotSupportedException("区域跨了显示器");
                local = new Rectangle(region.X - bounds.X, region.Y - bounds.Y, region.Width, region.Height);

                int level;
                Check(D3D11CreateDevice(adapter, 0 /*UNKNOWN*/, IntPtr.Zero, 0x20 /*BGRA*/, IntPtr.Zero, 0, 7, out device, out level, out context));
                Check(Marshal.QueryInterface(output, ref IID_IDXGIOutput1, out output1));
                Check(Call<DuplicateDel>(output1, 22)(output1, device, out dupl));

                var td = new D3D11_TEXTURE2D_DESC
                {
                    Width = region.Width, Height = region.Height, MipLevels = 1, ArraySize = 1, Format = 87 /*B8G8R8A8_UNORM*/,
                    SampleCount = 1, Usage = 3 /*STAGING*/, CPUAccessFlags = 0x20000 /*READ*/
                };
                Check(Call<CreateTexDel>(device, 5)(device, ref td, IntPtr.Zero, out staging));
            }
            catch { Dispose(); throw; }
            finally
            {
                if (output != IntPtr.Zero) Marshal.Release(output);
                if (adapter != IntPtr.Zero) Marshal.Release(adapter);
                Marshal.Release(factory);
            }
        }

        /// <summary>把区域画面拷进 dst（自上而下 BGRA）。返回 false 表示这一帧拿不到，调用方改用 GDI。</summary>
        public bool Grab(IntPtr dst, int dstStride)
        {
            if (dupl == IntPtr.Zero && !Reopen()) return hasContent && CopyOut(dst, dstStride);
            IntPtr resource;
            int hr = Call<AcquireDel>(dupl, 8)(dupl, hasContent ? 0 : 200, frameInfo, out resource);
            if (hr == DXGI_ERROR_ACCESS_LOST)
            {
                // 切换分辨率、UAC 弹窗、全屏游戏等会让复制失效：重新建立，期间沿用上一帧
                Marshal.Release(dupl); dupl = IntPtr.Zero;
                if (++lostCount > 300) throw new InvalidOperationException("显卡截屏连续失效");
                return hasContent && CopyOut(dst, dstStride);
            }
            if (hr == DXGI_ERROR_WAIT_TIMEOUT) return hasContent && CopyOut(dst, dstStride); // 画面没变
            if (hr < 0) return false;
            try
            {
                long lastPresent = Marshal.ReadInt64(frameInfo, 0);
                if (lastPresent != 0 || !hasContent)
                {
                    IntPtr tex;
                    Check(Marshal.QueryInterface(resource, ref IID_ID3D11Texture2D, out tex));
                    try
                    {
                        var box = new D3D11_BOX { left = local.Left, top = local.Top, front = 0, right = local.Right, bottom = local.Bottom, back = 1 };
                        Call<CopyRegionDel>(context, 46)(context, staging, 0, 0, 0, 0, tex, 0, ref box);
                        hasContent = true;
                        lostCount = 0;
                    }
                    finally { Marshal.Release(tex); }
                }
            }
            finally
            {
                Marshal.Release(resource);
                Call<ReleaseFrameDel>(dupl, 14)(dupl);
            }
            return hasContent && CopyOut(dst, dstStride);
        }

        bool Reopen()
        {
            return Call<DuplicateDel>(output1, 22)(output1, device, out dupl) >= 0;
        }

        bool CopyOut(IntPtr dst, int dstStride)
        {
            D3D11_MAPPED_SUBRESOURCE m;
            if (Call<MapDel>(context, 14)(context, staging, 0, 1 /*READ*/, 0, out m) < 0) return false;
            try
            {
                int row = local.Width * 4;
                for (int y = 0; y < local.Height; y++)
                    MF.CopyMemory(dst + y * dstStride, m.pData + y * m.RowPitch, (IntPtr)row);
            }
            finally { Call<UnmapDel>(context, 15)(context, staging, 0); }
            return true;
        }

        public void Dispose()
        {
            foreach (var p in new[] { staging, dupl, output1, context, device })
                if (p != IntPtr.Zero) Marshal.Release(p);
            staging = dupl = output1 = context = device = IntPtr.Zero;
            if (frameInfo != IntPtr.Zero) Marshal.FreeHGlobal(frameInfo);
            frameInfo = IntPtr.Zero;
        }

        static void Check(int hr) { if (hr < 0) Marshal.ThrowExceptionForHR(hr); }

        static T Call<T>(IntPtr obj, int slot) where T : class
        {
            IntPtr fn = Marshal.ReadIntPtr(Marshal.ReadIntPtr(obj), slot * IntPtr.Size);
            return (T)(object)Marshal.GetDelegateForFunctionPointer(fn, typeof(T));
        }

        // ---------- 结构和函数签名 ----------
        [StructLayout(LayoutKind.Sequential)]
        struct D3D11_TEXTURE2D_DESC
        {
            public int Width, Height, MipLevels, ArraySize, Format, SampleCount, SampleQuality, Usage, BindFlags, CPUAccessFlags, MiscFlags;
        }
        [StructLayout(LayoutKind.Sequential)] struct D3D11_BOX { public int left, top, front, right, bottom, back; }
        [StructLayout(LayoutKind.Sequential)] struct D3D11_MAPPED_SUBRESOURCE { public IntPtr pData; public int RowPitch, DepthPitch; }

        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int EnumDel(IntPtr self, int index, out IntPtr result);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int PtrDel(IntPtr self, IntPtr arg);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int DuplicateDel(IntPtr self, IntPtr device, out IntPtr dupl);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int CreateTexDel(IntPtr self, ref D3D11_TEXTURE2D_DESC desc, IntPtr init, out IntPtr tex);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int AcquireDel(IntPtr self, int timeoutMs, IntPtr frameInfo, out IntPtr resource);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int ReleaseFrameDel(IntPtr self);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate void CopyRegionDel(IntPtr self, IntPtr dst, int dstSub, int x, int y, int z, IntPtr src, int srcSub, ref D3D11_BOX box);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int MapDel(IntPtr self, IntPtr res, int sub, int mapType, int flags, out D3D11_MAPPED_SUBRESOURCE mapped);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate void UnmapDel(IntPtr self, IntPtr res, int sub);

        [DllImport("dxgi.dll")] static extern int CreateDXGIFactory1(ref Guid riid, out IntPtr factory);
        [DllImport("d3d11.dll")]
        static extern int D3D11CreateDevice(IntPtr adapter, int driverType, IntPtr software, int flags, IntPtr featureLevels, int numLevels,
                                            int sdkVersion, out IntPtr device, out int featureLevel, out IntPtr context);
    }
}
