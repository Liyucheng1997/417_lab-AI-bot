using System;
using System.Runtime.InteropServices;

namespace MySOP.Record
{
    /// <summary>
    /// Media Foundation 的最小 COM 封装：用系统自带的 Sink Writer 把画面编码成 H.264、声音编码成 AAC，写进 MP4。
    /// 不依赖 ffmpeg，有显卡硬件编码器时自动用上。
    /// 接口里的 _xx 方法只是占住 vtable 的位置（COM 接口按顺序调用），不会被调用。
    /// </summary>
    static class MF
    {
        const int MF_VERSION = 0x00020070;
        [DllImport("mfplat.dll", ExactSpelling = true)] static extern int MFStartup(int version, int flags);
        [DllImport("mfplat.dll", ExactSpelling = true)] static extern int MFShutdown();
        [DllImport("mfplat.dll", ExactSpelling = true)] static extern int MFCreateAttributes(out IMFAttributes attrs, int initialSize);
        [DllImport("mfplat.dll", ExactSpelling = true)] static extern int MFCreateMediaType(out IMFMediaType type);
        [DllImport("mfplat.dll", ExactSpelling = true)] static extern int MFCreateMemoryBuffer(int maxLength, out IMFMediaBuffer buffer);
        [DllImport("mfplat.dll", ExactSpelling = true)] static extern int MFCreateSample(out IMFSample sample);
        [DllImport("mfreadwrite.dll", ExactSpelling = true)]
        static extern int MFCreateSinkWriterFromURL([MarshalAs(UnmanagedType.LPWStr)] string url, IntPtr byteStream, IMFAttributes attrs, out IMFSinkWriter writer);
        [DllImport("kernel32.dll", EntryPoint = "RtlMoveMemory")] public static extern void CopyMemory(IntPtr dest, IntPtr src, IntPtr count);

        static void Check(int hr) { if (hr < 0) Marshal.ThrowExceptionForHR(hr); }

        public static void Startup() { Check(MFStartup(MF_VERSION, 0)); }
        public static void Shutdown() { MFShutdown(); }

        public static IMFAttributes Attributes() { IMFAttributes a; Check(MFCreateAttributes(out a, 4)); return a; }
        public static IMFMediaType MediaType() { IMFMediaType t; Check(MFCreateMediaType(out t)); return t; }
        public static IMFMediaBuffer Buffer(int size) { IMFMediaBuffer b; Check(MFCreateMemoryBuffer(size, out b)); return b; }
        public static IMFSample Sample() { IMFSample s; Check(MFCreateSample(out s)); return s; }
        public static IMFSinkWriter SinkWriter(string path, IMFAttributes attrs)
        {
            IMFSinkWriter w; Check(MFCreateSinkWriterFromURL(path, IntPtr.Zero, attrs, out w)); return w;
        }

        public static void Release(object o) { if (o != null && Marshal.IsComObject(o)) Marshal.ReleaseComObject(o); }

        // ---------- GUID ----------
        static Guid FourCC(uint code) { return new Guid(code, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71); }

        public static Guid MT_MAJOR_TYPE = new Guid("48eba18e-f8c9-4687-bf11-0a74c9f96a8f");
        public static Guid MT_SUBTYPE = new Guid("f7e34c9a-42e8-4714-b74b-cb29d72c35e5");
        public static Guid MediaType_Video = FourCC(0x73646976); // 'vids'
        public static Guid MediaType_Audio = FourCC(0x73647561); // 'auds'
        public static Guid VideoFormat_H264 = FourCC(0x34363248); // 'H264'
        public static Guid VideoFormat_RGB32 = FourCC(22);        // D3DFMT_X8R8G8B8
        public static Guid AudioFormat_AAC = FourCC(0x1610);
        public static Guid AudioFormat_PCM = FourCC(1);

        public static Guid MT_AVG_BITRATE = new Guid("20332624-fb0d-4d9e-bd0d-cbf6786c102e");
        public static Guid MT_INTERLACE_MODE = new Guid("e2724bb8-e676-4806-b4b2-a8d6efb44ccd");
        public static Guid MT_FRAME_SIZE = new Guid("1652c33d-d6b2-4012-b834-72030849a37d");
        public static Guid MT_FRAME_RATE = new Guid("c459a2e8-3d2c-4e44-b132-fee5156c7bb0");
        public static Guid MT_PIXEL_ASPECT_RATIO = new Guid("c6376a1e-8d0a-4027-be45-6d9a0ad39bb6");
        public static Guid MT_DEFAULT_STRIDE = new Guid("644b4e48-1e02-4516-b0eb-c01ca9d49ac6");
        public static Guid MT_MPEG2_PROFILE = new Guid("ad76a80b-2d5c-4e0b-b375-64e520137036");
        public static Guid MT_AUDIO_NUM_CHANNELS = new Guid("37e48bf5-645e-4c5b-89de-ada9e29b696a");
        public static Guid MT_AUDIO_SAMPLES_PER_SECOND = new Guid("5faeeae7-0290-4c31-9e8a-c534f68d9dba");
        public static Guid MT_AUDIO_BITS_PER_SAMPLE = new Guid("f2deb57f-40fa-4764-aa33-ed4f2d1ff669");
        public static Guid MT_AUDIO_BLOCK_ALIGNMENT = new Guid("322de230-9eeb-43bd-ab7a-ff412251541d");
        public static Guid MT_AUDIO_AVG_BYTES_PER_SECOND = new Guid("1aab75c8-cfef-451c-ab95-ac034b8e1731");
        public static Guid READWRITE_ENABLE_HARDWARE_TRANSFORMS = new Guid("a634a91c-822b-41b9-a494-4de4643612b0");
        public static Guid SINK_WRITER_DISABLE_THROTTLING = new Guid("08b845d8-2b74-4afe-9d53-be16d2d5ae4f");
    }

    [ComImport, Guid("2cd2d921-c447-44a7-a13c-4adabfc247e3"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMFAttributes
    {
        void _00(); void _01(); void _02(); void _03(); void _04(); void _05(); void _06(); void _07(); void _08(); void _09();
        void _10(); void _11(); void _12(); void _13(); void _14(); void _15(); void _16(); void _17();
        void SetUINT32([In] ref Guid key, int value);
        void SetUINT64([In] ref Guid key, long value);
        void _20();
        void SetGUID([In] ref Guid key, [In] ref Guid value);
        void _22(); void _23(); void _24(); void _25(); void _26(); void _27(); void _28(); void _29();
    }

    [ComImport, Guid("44ae0fa8-ea31-4109-8d2e-4cae4997c555"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMFMediaType
    {
        void _00(); void _01(); void _02(); void _03(); void _04(); void _05(); void _06(); void _07(); void _08(); void _09();
        void _10(); void _11(); void _12(); void _13(); void _14(); void _15(); void _16(); void _17();
        void SetUINT32([In] ref Guid key, int value);
        void SetUINT64([In] ref Guid key, long value);
        void _20();
        void SetGUID([In] ref Guid key, [In] ref Guid value);
        void _22(); void _23(); void _24(); void _25(); void _26(); void _27(); void _28(); void _29();
        // IMFMediaType 自己的 5 个方法（用不到）
        void _30(); void _31(); void _32(); void _33(); void _34();
    }

    [ComImport, Guid("c40a00f2-b93a-4d80-ae8c-5a1c634f58e4"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMFSample
    {
        void _00(); void _01(); void _02(); void _03(); void _04(); void _05(); void _06(); void _07(); void _08(); void _09();
        void _10(); void _11(); void _12(); void _13(); void _14(); void _15(); void _16(); void _17(); void _18(); void _19();
        void _20(); void _21(); void _22(); void _23(); void _24(); void _25(); void _26(); void _27(); void _28(); void _29();
        void GetSampleFlags(out int flags);
        void SetSampleFlags(int flags);
        void GetSampleTime(out long time);
        void SetSampleTime(long time);
        void GetSampleDuration(out long duration);
        void SetSampleDuration(long duration);
        void GetBufferCount(out int count);
        void _37();
        void _38();
        void AddBuffer(IMFMediaBuffer buffer);
    }

    [ComImport, Guid("045fa593-8799-42b8-bc8d-8968c6453507"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMFMediaBuffer
    {
        void Lock(out IntPtr buffer, out int maxLength, out int currentLength);
        void Unlock();
        void GetCurrentLength(out int length);
        void SetCurrentLength(int length);
        void GetMaxLength(out int length);
    }

    [ComImport, Guid("3137f1cd-fe5e-4805-a5d8-fb477448cb3d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMFSinkWriter
    {
        void AddStream(IMFMediaType targetType, out int streamIndex);
        void SetInputMediaType(int streamIndex, IMFMediaType inputType, IMFAttributes encodingParameters);
        void BeginWriting();
        void WriteSample(int streamIndex, IMFSample sample);
        void SendStreamTick(int streamIndex, long timestamp);
        void _PlaceMarker();
        void _NotifyEndOfSegment();
        void _Flush();
        void DoFinalize();
    }

    /// <summary>MP4 写入器：一路 H.264 画面（输入 32 位 BGRA，自上而下），可选一路 AAC 声音（输入 16 位 PCM）。</summary>
    sealed class Mp4Writer : IDisposable
    {
        readonly IMFSinkWriter writer;
        readonly int video = -1, audio = -1;
        readonly int width, height, rowBytes;
        readonly object gate = new object();
        public readonly int AudioRate, AudioChannels;
        bool finished;

        public Mp4Writer(string path, int width, int height, int fps, int bitrate, int audioRate, int audioChannels)
        {
            this.width = width; this.height = height; rowBytes = width * 4;
            AudioRate = audioRate; AudioChannels = audioChannels;
            MF.Startup();
            IMFAttributes attrs = null;
            IMFMediaType vOut = null, vIn = null, aOut = null, aIn = null;
            try
            {
                attrs = MF.Attributes();
                attrs.SetUINT32(ref MF.READWRITE_ENABLE_HARDWARE_TRANSFORMS, 1);
                // 画面和声音在两个线程写：不让 WriteSample 阻塞，免得两路互相等
                attrs.SetUINT32(ref MF.SINK_WRITER_DISABLE_THROTTLING, 1);
                writer = MF.SinkWriter(path, attrs);

                long size = ((long)width << 32) | (uint)height;
                long rate = ((long)fps << 32) | 1u;
                long par = (1L << 32) | 1u;

                vOut = MF.MediaType();
                vOut.SetGUID(ref MF.MT_MAJOR_TYPE, ref MF.MediaType_Video);
                vOut.SetGUID(ref MF.MT_SUBTYPE, ref MF.VideoFormat_H264);
                vOut.SetUINT32(ref MF.MT_AVG_BITRATE, bitrate);
                vOut.SetUINT32(ref MF.MT_INTERLACE_MODE, 2); // 逐行
                vOut.SetUINT64(ref MF.MT_FRAME_SIZE, size);
                vOut.SetUINT64(ref MF.MT_FRAME_RATE, rate);
                vOut.SetUINT64(ref MF.MT_PIXEL_ASPECT_RATIO, par);
                vOut.SetUINT32(ref MF.MT_MPEG2_PROFILE, 100); // High：屏幕上的文字更清楚
                int vs; writer.AddStream(vOut, out vs); video = vs;

                vIn = MF.MediaType();
                vIn.SetGUID(ref MF.MT_MAJOR_TYPE, ref MF.MediaType_Video);
                vIn.SetGUID(ref MF.MT_SUBTYPE, ref MF.VideoFormat_RGB32);
                vIn.SetUINT32(ref MF.MT_INTERLACE_MODE, 2);
                vIn.SetUINT64(ref MF.MT_FRAME_SIZE, size);
                vIn.SetUINT64(ref MF.MT_FRAME_RATE, rate);
                vIn.SetUINT64(ref MF.MT_PIXEL_ASPECT_RATIO, par);
                vIn.SetUINT32(ref MF.MT_DEFAULT_STRIDE, rowBytes); // 正数 = 自上而下
                writer.SetInputMediaType(video, vIn, null);

                if (audioRate > 0)
                {
                    aOut = MF.MediaType();
                    aOut.SetGUID(ref MF.MT_MAJOR_TYPE, ref MF.MediaType_Audio);
                    aOut.SetGUID(ref MF.MT_SUBTYPE, ref MF.AudioFormat_AAC);
                    aOut.SetUINT32(ref MF.MT_AUDIO_BITS_PER_SAMPLE, 16);
                    aOut.SetUINT32(ref MF.MT_AUDIO_SAMPLES_PER_SECOND, audioRate);
                    aOut.SetUINT32(ref MF.MT_AUDIO_NUM_CHANNELS, audioChannels);
                    aOut.SetUINT32(ref MF.MT_AUDIO_AVG_BYTES_PER_SECOND, 24000); // 192 kbps
                    int a; writer.AddStream(aOut, out a); audio = a;

                    aIn = MF.MediaType();
                    aIn.SetGUID(ref MF.MT_MAJOR_TYPE, ref MF.MediaType_Audio);
                    aIn.SetGUID(ref MF.MT_SUBTYPE, ref MF.AudioFormat_PCM);
                    aIn.SetUINT32(ref MF.MT_AUDIO_BITS_PER_SAMPLE, 16);
                    aIn.SetUINT32(ref MF.MT_AUDIO_SAMPLES_PER_SECOND, audioRate);
                    aIn.SetUINT32(ref MF.MT_AUDIO_NUM_CHANNELS, audioChannels);
                    aIn.SetUINT32(ref MF.MT_AUDIO_BLOCK_ALIGNMENT, audioChannels * 2);
                    aIn.SetUINT32(ref MF.MT_AUDIO_AVG_BYTES_PER_SECOND, audioRate * audioChannels * 2);
                    writer.SetInputMediaType(audio, aIn, null);
                }
                writer.BeginWriting();
            }
            catch
            {
                MF.Release(writer);
                MF.Shutdown();
                throw;
            }
            finally
            {
                MF.Release(attrs); MF.Release(vOut); MF.Release(vIn); MF.Release(aOut); MF.Release(aIn);
            }
        }

        public bool HasAudio { get { return audio >= 0; } }

        /// <summary>写一帧画面。pixels：自上而下的 BGRA，每行 stride 字节。时间单位 100 纳秒。</summary>
        public void WriteVideo(IntPtr pixels, int stride, long time, long duration)
        {
            int len = rowBytes * height;
            var buf = MF.Buffer(len);
            IMFSample sample = null;
            try
            {
                IntPtr dst; int max, cur;
                buf.Lock(out dst, out max, out cur);
                try
                {
                    if (stride == rowBytes) MF.CopyMemory(dst, pixels, (IntPtr)len);
                    else
                        for (int y = 0; y < height; y++)
                            MF.CopyMemory(dst + y * rowBytes, pixels + y * stride, (IntPtr)rowBytes);
                }
                finally { buf.Unlock(); }
                buf.SetCurrentLength(len);
                sample = MF.Sample();
                sample.AddBuffer(buf);
                sample.SetSampleTime(time);
                sample.SetSampleDuration(duration);
                lock (gate) { if (!finished) writer.WriteSample(video, sample); }
            }
            finally { MF.Release(sample); MF.Release(buf); }
        }

        /// <summary>写一段声音：16 位交错 PCM。</summary>
        public void WriteAudio(byte[] pcm, int count, long time)
        {
            if (audio < 0 || count <= 0) return;
            var buf = MF.Buffer(count);
            IMFSample sample = null;
            try
            {
                IntPtr dst; int max, cur;
                buf.Lock(out dst, out max, out cur);
                try { Marshal.Copy(pcm, 0, dst, count); }
                finally { buf.Unlock(); }
                buf.SetCurrentLength(count);
                sample = MF.Sample();
                sample.AddBuffer(buf);
                sample.SetSampleTime(time);
                sample.SetSampleDuration((long)count / (AudioChannels * 2) * 10000000L / AudioRate);
                lock (gate) { if (!finished) writer.WriteSample(audio, sample); }
            }
            finally { MF.Release(sample); MF.Release(buf); }
        }

        /// <summary>收尾：写 MP4 索引。录得越长越久（一般不到一秒）。</summary>
        public void Finish()
        {
            lock (gate)
            {
                if (finished) return;
                finished = true;
                try { writer.DoFinalize(); }
                finally { MF.Release(writer); MF.Shutdown(); }
            }
        }

        public void Dispose()
        {
            lock (gate)
            {
                if (finished) return;
                finished = true;
                MF.Release(writer);
                MF.Shutdown();
            }
        }
    }
}
