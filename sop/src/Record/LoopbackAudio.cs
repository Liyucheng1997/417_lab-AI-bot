using System;
using System.Runtime.InteropServices;

namespace MySOP.Record
{
    /// <summary>
    /// 录电脑正在播放的声音（WASAPI 环回，默认播放设备）。输出统一成 16 位交错 PCM、双声道、44.1/48 kHz。
    /// 没有声音在播放时系统不给数据，由调用方按时间补静音。
    /// </summary>
    sealed class LoopbackAudio : IDisposable
    {
        const int AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000;
        const int AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = unchecked((int)0x80000000);
        const int AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000;
        const int AUDCLNT_BUFFERFLAGS_SILENT = 2;
        const int CLSCTX_ALL = 0x17;
        static readonly Guid IID_IAudioClient = new Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2");
        static readonly Guid IID_IAudioCaptureClient = new Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317");
        static readonly Guid SubFormatFloat = new Guid("00000003-0000-0010-8000-00aa00389b71");
        static readonly Guid SubFormatPcm = new Guid("00000001-0000-0010-8000-00aa00389b71");

        IMMDevice device;
        IAudioClient client;
        IAudioCaptureClient capture;
        public readonly int SampleRate;
        public const int Channels = 2;
        // 设备给的原始格式（自动转换失败时自己转）
        bool srcFloat;
        int srcChannels, srcBits, srcBlock;
        byte[] outBuf = new byte[0];

        public LoopbackAudio()
        {
            var en = (IMMDeviceEnumerator)new MMDeviceEnumeratorCom();
            try { Check(en.GetDefaultAudioEndpoint(0 /*eRender*/, 1 /*eMultimedia*/, out device)); }
            finally { Marshal.ReleaseComObject(en); }

            client = Activate();
            IntPtr mixPtr;
            Check(client.GetMixFormat(out mixPtr));
            int mixRate = Marshal.ReadInt32(mixPtr, 4);
            ushort tag = (ushort)Marshal.ReadInt16(mixPtr, 0);
            srcChannels = Marshal.ReadInt16(mixPtr, 2);
            srcBlock = Marshal.ReadInt16(mixPtr, 12);
            srcBits = Marshal.ReadInt16(mixPtr, 14);
            srcFloat = tag == 3;
            if (tag == 0xFFFE)
            {
                var sub = (Guid)Marshal.PtrToStructure(mixPtr + 24, typeof(Guid));
                srcFloat = sub == SubFormatFloat;
                if (!srcFloat && sub != SubFormatPcm) srcBits = 0;
            }

            // 1) 首选：让系统直接给 16 位双声道（AAC 编码器只认 44.1k / 48k）
            SampleRate = mixRate == 44100 ? 44100 : 48000;
            var want = new WAVEFORMATEX
            {
                wFormatTag = 1, nChannels = Channels, nSamplesPerSec = SampleRate, wBitsPerSample = 16,
                nBlockAlign = Channels * 2, nAvgBytesPerSec = SampleRate * Channels * 2, cbSize = 0
            };
            IntPtr wantPtr = Marshal.AllocCoTaskMem(Marshal.SizeOf(want));
            int hr;
            try
            {
                Marshal.StructureToPtr(want, wantPtr, false);
                hr = client.Initialize(0, AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                                       10000000, 0, wantPtr, IntPtr.Zero);
            }
            finally { Marshal.FreeCoTaskMem(wantPtr); }

            if (hr >= 0) { srcFloat = false; srcChannels = Channels; srcBits = 16; srcBlock = Channels * 2; }
            else
            {
                // 2) 退路：用设备的混音格式，自己转成 16 位双声道
                Marshal.ReleaseComObject(client);
                client = Activate();
                if (mixRate != 44100 && mixRate != 48000) { Marshal.FreeCoTaskMem(mixPtr); throw new NotSupportedException("声卡采样率 " + mixRate + " Hz，不支持"); }
                if (!(srcFloat && srcBits == 32) && srcBits != 16) { Marshal.FreeCoTaskMem(mixPtr); throw new NotSupportedException("声卡输出格式不支持"); }
                hr = client.Initialize(0, AUDCLNT_STREAMFLAGS_LOOPBACK, 10000000, 0, mixPtr, IntPtr.Zero);
                if (hr < 0) { Marshal.FreeCoTaskMem(mixPtr); Marshal.ThrowExceptionForHR(hr); }
            }
            Marshal.FreeCoTaskMem(mixPtr);

            object o;
            var iid = IID_IAudioCaptureClient;
            Check(client.GetService(ref iid, out o));
            capture = (IAudioCaptureClient)o;
        }

        IAudioClient Activate()
        {
            object o;
            var iid = IID_IAudioClient;
            Check(device.Activate(ref iid, CLSCTX_ALL, IntPtr.Zero, out o));
            return (IAudioClient)o;
        }

        static void Check(int hr) { if (hr < 0) Marshal.ThrowExceptionForHR(hr); }

        public void Start() { Check(client.Start()); }

        /// <summary>
        /// 取出目前缓冲里的所有声音。每个数据包回调一次：(16 位 PCM 数据, 字节数, 是否静音包)。
        /// </summary>
        public void Drain(Action<byte[], int> onData)
        {
            int packet;
            while (capture.GetNextPacketSize(out packet) >= 0 && packet > 0)
            {
                IntPtr data; int frames, flags; long pos, qpc;
                Check(capture.GetBuffer(out data, out frames, out flags, out pos, out qpc));
                try
                {
                    int bytes = frames * Channels * 2;
                    if (outBuf.Length < bytes) outBuf = new byte[Math.Max(bytes, outBuf.Length * 2)];
                    if ((flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0 || data == IntPtr.Zero) Array.Clear(outBuf, 0, bytes);
                    else Convert(data, frames);
                    onData(outBuf, bytes);
                }
                finally { capture.ReleaseBuffer(frames); }
            }
        }

        void Convert(IntPtr src, int frames)
        {
            if (!srcFloat && srcBits == 16 && srcChannels == Channels) { Marshal.Copy(src, outBuf, 0, frames * srcBlock); return; }
            int o = 0;
            for (int f = 0; f < frames; f++)
            {
                IntPtr p = src + f * srcBlock;
                for (int c = 0; c < Channels; c++)
                {
                    int sc = Math.Min(c, srcChannels - 1);
                    short v;
                    if (srcFloat)
                    {
                        float x = BitConverter.ToSingle(BitConverter.GetBytes(Marshal.ReadInt32(p, sc * 4)), 0);
                        if (x > 1f) x = 1f; else if (x < -1f) x = -1f;
                        v = (short)(x * 32767f);
                    }
                    else v = Marshal.ReadInt16(p, sc * 2);
                    outBuf[o++] = (byte)v;
                    outBuf[o++] = (byte)(v >> 8);
                }
            }
        }

        public void Dispose()
        {
            try { if (client != null) client.Stop(); } catch { }
            if (capture != null) Marshal.ReleaseComObject(capture);
            if (client != null) Marshal.ReleaseComObject(client);
            if (device != null) Marshal.ReleaseComObject(device);
            capture = null; client = null; device = null;
        }

        // ---------- COM ----------
        [StructLayout(LayoutKind.Sequential, Pack = 2)]
        struct WAVEFORMATEX
        {
            public short wFormatTag, nChannels;
            public int nSamplesPerSec, nAvgBytesPerSec;
            public short nBlockAlign, wBitsPerSample, cbSize;
        }

        [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
        class MMDeviceEnumeratorCom { }

        [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IMMDeviceEnumerator
        {
            [PreserveSig] int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr devices);
            [PreserveSig] int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
        }

        [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IMMDevice
        {
            [PreserveSig] int Activate([In] ref Guid iid, int clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
        }

        [ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IAudioClient
        {
            [PreserveSig] int Initialize(int shareMode, int streamFlags, long bufferDuration, long periodicity, IntPtr format, IntPtr sessionGuid);
            [PreserveSig] int GetBufferSize(out int frames);
            [PreserveSig] int GetStreamLatency(out long latency);
            [PreserveSig] int GetCurrentPadding(out int frames);
            [PreserveSig] int IsFormatSupported(int shareMode, IntPtr format, out IntPtr closest);
            [PreserveSig] int GetMixFormat(out IntPtr format);
            [PreserveSig] int GetDevicePeriod(out long defaultPeriod, out long minPeriod);
            [PreserveSig] int Start();
            [PreserveSig] int Stop();
            [PreserveSig] int Reset();
            [PreserveSig] int SetEventHandle(IntPtr handle);
            [PreserveSig] int GetService([In] ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
        }

        [ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IAudioCaptureClient
        {
            [PreserveSig] int GetBuffer(out IntPtr data, out int frames, out int flags, out long devicePosition, out long qpcPosition);
            [PreserveSig] int ReleaseBuffer(int frames);
            [PreserveSig] int GetNextPacketSize(out int frames);
        }
    }
}
