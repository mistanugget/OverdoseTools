import struct, os, sys, json, shutil
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

MARKER = b'\x0d\xf0\xad\xba'
PCM_PS2 = b'\x00\xfb\xaf\xba\x00\x00\x00\x00'
MARKER_LEN = 4
AUDIO_HDR = 60
HDR_GAP_PS2 = 24
HDR_GAP_PC = 20
CAT_STREAM_PS2 = {0x400000, 0x500000, 0x600000}
CAT_WAV_PS2 = {0x300000, 0x400000}
CAT_STREAM_PC = {0x40, 0x50, 0x60}
CAT_WAV_PC = {0x30, 0x40}

def find_pc_meta(raw):
    null = raw.find(b'\x00', MARKER_LEN)
    if null <= MARKER_LEN:
        return -1
    pa4 = ((null + 3) // 4) * 4
    return pa4 + 16 if pa4 % 16 == 0 else pa4 + 4

def read_meta_pc(raw, mo):
    sr = struct.unpack_from('<I', raw, mo)[0]
    ch = struct.unpack_from('<H', raw, mo + 4)[0]
    bc = struct.unpack_from('<I', raw, mo + 12)[0]
    return sr, ch, bc

def build_wav(pcm_data, sr, ch):
    bps = 16
    ba = ch * (bps // 8)
    br = sr * ba
    ds = len(pcm_data)
    wav = bytearray()
    wav += b'RIFF'
    wav += struct.pack('<I', 36 + ds)
    wav += b'WAVE'
    wav += b'fmt '
    wav += struct.pack('<I', 16)
    wav += struct.pack('<H', 1)
    wav += struct.pack('<H', ch)
    wav += struct.pack('<I', sr)
    wav += struct.pack('<I', br)
    wav += struct.pack('<H', ba)
    wav += struct.pack('<H', bps)
    wav += b'data'
    wav += struct.pack('<I', ds)
    wav += pcm_data
    return bytes(wav)

def wav_to_pcm(wav_bytes):
    fo = wav_bytes.find(b'fmt ', 12)
    if fo < 0:
        return None
    fe = fo + 8 + struct.unpack_from('<I', wav_bytes, fo + 4)[0]
    do = wav_bytes.find(b'data', fe)
    if do < 0:
        return None
    ds = struct.unpack_from('<I', wav_bytes, do + 4)[0]
    return bytes(wav_bytes[do + 8:do + 8 + ds])

def read_wav_fmt(wav_bytes):
    fo = wav_bytes.find(b'fmt ', 12)
    if fo < 0:
        return 22050, 1, 16
    sr = struct.unpack_from('<I', wav_bytes, fo + 12)[0]
    ch = struct.unpack_from('<H', wav_bytes, fo + 10)[0]
    bps = struct.unpack_from('<H', wav_bytes, fo + 22)[0]
    return sr, ch, bps

def downsample_pcm(pcm, orig_sr, orig_ch, target_size):
    if len(pcm) <= target_size:
        return pcm, orig_sr, orig_ch
    bps = 16
    bpf = orig_ch * (bps // 8)
    for n in range(2, 20):
        new_size = len(pcm) // n
        if new_size % bpf != 0:
            new_size -= new_size % bpf
        if new_size <= target_size:
            new_sr = orig_sr // n
            if new_sr < 8000:
                continue
            new_pcm = bytearray()
            for i in range(0, len(pcm) - (len(pcm) % bpf), n * bpf):
                new_pcm.extend(pcm[i:i + bpf])
            return bytes(new_pcm), new_sr, orig_ch
    return pcm[:target_size - (target_size % bpf)], orig_sr // 10, orig_ch

class StreamEntry:
    def __init__(self, hdr_off, path_off, data_off, path, volume, pitch, speed,
                 unk1, unk2, unk3, unk4, flags, stream_idx, is_pc, ogg_ref=''):
        self.hdr_off = hdr_off
        self.path_off = path_off
        self.data_off = data_off
        self.path = path
        self.volume = volume
        self.pitch = pitch
        self.speed = speed
        self.unk1 = unk1
        self.unk2 = unk2
        self.unk3 = unk3
        self.unk4 = unk4
        self.flags = flags
        self.stream_idx = stream_idx
        self.is_pc = is_pc
        self.ogg_ref = ogg_ref
        self.data_gap = 0

class WAVEntry:
    def __init__(self, hdr_off, wav_off, gap, path, sr, ch, bc, pcm_start, mo):
        self.hdr_off = hdr_off
        self.wav_off = wav_off
        self.gap = gap
        self.path = path
        self.sr = sr
        self.ch = ch
        self.bc = bc
        self.pcm_start = pcm_start
        self.mo = mo

class EditStreamDialog(tk.Toplevel):
    def __init__(self, parent, entry):
        super().__init__(parent)
        self.result = None
        self.entry = entry
        self.title(f'Edit Stream {entry.stream_idx}')
        self.transient(parent)
        self.grab_set()
        w = ttk.Frame(self, padding=10)
        w.pack(fill='both', expand=True)
        fields = [
            ('Path', entry.path),
            ('Volume', str(entry.volume)),
            ('Pitch', str(entry.pitch)),
            ('Speed', str(entry.speed)),
        ]
        self.widgets = {}
        for i, (label, val) in enumerate(fields):
            ttk.Label(w, text=label).grid(row=i, column=0, sticky='w', pady=2)
            var = tk.StringVar(value=val)
            ent = ttk.Entry(w, textvariable=var, width=60)
            ent.grid(row=i, column=1, sticky='ew', padx=5, pady=2)
            self.widgets[label] = var
        bf = ttk.Frame(w)
        bf.grid(row=len(fields), column=0, columnspan=2, pady=10)
        ttk.Button(bf, text='OK', command=self.on_ok).pack(side='left', padx=5)
        ttk.Button(bf, text='Cancel', command=self.destroy).pack(side='left', padx=5)
        w.columnconfigure(1, weight=1)
        self.bind('<Return>', lambda e: self.on_ok())
        self.bind('<Escape>', lambda e: self.destroy())

    def on_ok(self):
        try:
            vol = float(self.widgets['Volume'].get())
            pitch = float(self.widgets['Pitch'].get())
            speed = float(self.widgets['Speed'].get())
        except ValueError:
            messagebox.showerror('Error', 'Volume/Pitch/Speed must be numbers')
            return
        self.result = {
            'path': self.widgets['Path'].get(),
            'volume': vol, 'pitch': pitch, 'speed': speed,
        }
        self.destroy()

class WAVFilter:
    def filter(self, row_data, text):
        return True

class ODUKGUI:
    def __init__(self):
        self.win = tk.Tk()
        self.win.title('TOD Audio Editor')
        self.win.geometry('850x550')

        style = ttk.Style()
        style.theme_use('clam')
        bg = '#000000'
        fg = '#ffffff'
        sel = '#333333'
        style.configure('.', background=bg, foreground=fg, fieldbackground=bg, selectbackground=sel)
        style.configure('TFrame', background=bg)
        style.configure('TLabel', background=bg, foreground=fg)
        style.configure('TButton', background='#222222', foreground=fg, bordercolor='#444444')
        style.map('TButton', background=[('active', '#444444')])
        style.configure('Treeview', background='#111111', foreground=fg, fieldbackground='#111111',
                        selectbackground=sel, selectforeground=fg, bordercolor='#222222')
        style.map('Treeview', background=[('selected', sel)])
        style.configure('Treeview.Heading', background='#222222', foreground=fg, bordercolor='#333333')
        self.win.configure(bg=bg)
        style.configure('TNotebook', background=bg, bordercolor='#333333')
        style.configure('TNotebook.Tab', background='#222222', foreground=fg, bordercolor='#333333')
        style.map('TNotebook.Tab', background=[('selected', bg)], foreground=[('selected', fg)])
        style.configure('Vertical.TScrollbar', background='#222222', bordercolor='#333333', arrowcolor=fg)
        style.map('Vertical.TScrollbar', background=[('active', '#444444')])

        if getattr(sys, 'frozen', False):
            base = sys._MEIPASS
        else:
            base = os.path.dirname(__file__)
        ico = Path(base) / 'Icon2.ico'
        if ico.exists():
            try:
                self.win.iconbitmap(str(ico))
            except Exception:
                pass

        self.archive_path = None
        self.archive_data = None
        self.wav_entries = []
        self.stream_entries = []
        self.fmt = 'pc'
        self.all_wav_rows = []

        f_top = ttk.Frame(self.win)
        f_top.pack(fill='x', padx=5, pady=5)
        ttk.Button(f_top, text='Open', command=self.open_file).pack(side='left', padx=2)
        self.lbl = ttk.Label(f_top, text='No archive loaded')
        self.lbl.pack(side='left', padx=10)
        ttk.Button(f_top, text='Save As...', command=self.save_as).pack(side='right', padx=2)

        self.nb = ttk.Notebook(self.win)
        self.nb.pack(fill='both', expand=True, padx=5, pady=5)

        self.wav_frame = ttk.Frame(self.nb)
        self.nb.add(self.wav_frame, text='WAVs')

        filter_frame = ttk.Frame(self.wav_frame)
        filter_frame.pack(fill='x', pady=(0, 5))
        ttk.Label(filter_frame, text='Filter:').pack(side='left', padx=2)
        self.filter_var = tk.StringVar()
        self.filter_var.trace('w', lambda *a: self.apply_filter())
        filter_entry = ttk.Entry(filter_frame, textvariable=self.filter_var)
        filter_entry.pack(side='left', fill='x', expand=True, padx=5)

        wav_cols = ('path', 'sr_ch', 'offset')
        self.wav_tree = ttk.Treeview(self.wav_frame, columns=wav_cols, show='headings', selectmode='browse')
        self.wav_tree.heading('path', text='Path')
        self.wav_tree.heading('sr_ch', text='Format')
        self.wav_tree.heading('offset', text='Offset')
        self.wav_tree.column('path', width=450)
        self.wav_tree.column('sr_ch', width=120)
        self.wav_tree.column('offset', width=100)
        wav_sb = ttk.Scrollbar(self.wav_frame, orient='vertical', command=self.wav_tree.yview)
        self.wav_tree.configure(yscrollcommand=wav_sb.set)
        self.wav_tree.pack(fill='both', expand=True)
        wav_sb.pack(side='right', fill='y')

        wav_bf = ttk.Frame(self.wav_frame)
        wav_bf.pack(fill='x', pady=5)
        ttk.Button(wav_bf, text='Extract WAV', command=self.extract_wav).pack(side='left', padx=2)
        ttk.Button(wav_bf, text='Replace WAV', command=self.replace_wav).pack(side='left', padx=2)

        self.str_frame = ttk.Frame(self.nb)
        self.nb.add(self.str_frame, text='Streams')

        str_cols = ('idx', 'path', 'vol', 'pitch', 'speed', 'ogg', 'hdr_off')
        self.str_tree = ttk.Treeview(self.str_frame, columns=str_cols, show='headings', selectmode='browse')
        self.str_tree.heading('idx', text='#')
        self.str_tree.heading('path', text='Path')
        self.str_tree.heading('vol', text='Volume')
        self.str_tree.heading('pitch', text='Pitch')
        self.str_tree.heading('speed', text='Speed')
        self.str_tree.heading('ogg', text='OGG Ref')
        self.str_tree.heading('hdr_off', text='HDR Offset')
        self.str_tree.column('idx', width=40)
        self.str_tree.column('path', width=300)
        self.str_tree.column('vol', width=70)
        self.str_tree.column('pitch', width=70)
        self.str_tree.column('speed', width=70)
        self.str_tree.column('ogg', width=140)
        self.str_tree.column('hdr_off', width=80)
        str_sb = ttk.Scrollbar(self.str_frame, orient='vertical', command=self.str_tree.yview)
        self.str_tree.configure(yscrollcommand=str_sb.set)
        self.str_tree.pack(fill='both', expand=True)
        str_sb.pack(side='right', fill='y')

        str_bf = ttk.Frame(self.str_frame)
        str_bf.pack(fill='x', pady=5)
        ttk.Button(str_bf, text='Edit Stream', command=self.edit_stream).pack(side='left', padx=2)
        ttk.Button(str_bf, text='Edit OGG', command=self.edit_ogg).pack(side='left', padx=2)
        ttk.Button(str_bf, text='Dump List', command=self.dump_streams).pack(side='left', padx=2)

        self.win.mainloop()

    def open_file(self):
        p = filedialog.askopenfilename(title='Open overdose_uk.main', filetypes=[('Main archive', '*.main'), ('All files', '*.*')])
        if not p:
            return
        self.archive_path = p
        with open(p, 'rb') as f:
            self.archive_data = bytearray(f.read())
        self.parse()
        tree_streams = len(self.str_tree.get_children())
        tree_wavs = len(self.wav_tree.get_children())
        if len(self.stream_entries) != tree_streams:
            msg = f'stream list={len(self.stream_entries)} tree={tree_streams}'
            tree_ids = set(self.str_tree.get_children())
            for si, se in enumerate(self.stream_entries):
                if f's{si}' not in tree_ids:
                    msg += f'\n  missing idx={si} path={se.path}'
            messagebox.showwarning('Mismatch', msg)
        if len(self.wav_entries) != tree_wavs:
            messagebox.showwarning('Mismatch', f'wav list={len(self.wav_entries)} tree={tree_wavs}')
        self.lbl.config(text=f'{Path(p).name} ({len(self.archive_data)} bytes, {tree_wavs} WAVs, {tree_streams} streams)')

    def save_as(self):
        if self.archive_data is None:
            return
        p = filedialog.asksaveasfilename(title='Save archive as', defaultextension='.main')
        if not p:
            return
        with open(p, 'wb') as f:
            f.write(self.archive_data)
        messagebox.showinfo('Saved', f'Saved to {Path(p).name}')

    def apply_filter(self):
        text = self.filter_var.get().lower()
        self.wav_tree.delete(*self.wav_tree.get_children())
        for row in self.all_wav_rows:
            if text in row['path'].lower() or not text:
                self.wav_tree.insert('', 'end', iid=row['iid'],
                    values=(row['path'], row['format'], row['offset']))

    def dump_streams(self):
        if not self.stream_entries:
            messagebox.showwarning('No streams', 'Load an archive first')
            return
        p = filedialog.asksaveasfilename(title='Save stream list', defaultextension='.txt')
        if not p:
            return
        with open(p, 'w') as f:
            for se in self.stream_entries:
                f.write(f'{se.stream_idx:4d}  {se.path}\n')
        messagebox.showinfo('Done', f'{len(self.stream_entries)} streams -> {Path(p).name}')

    def _detect_fmt(self, offs):
        for i in range(len(offs) - 1):
            gap = offs[i + 1] - offs[i] - MARKER_LEN
            if gap == HDR_GAP_PS2:
                return 'ps2'
            elif gap == HDR_GAP_PC:
                return 'pc'
        return 'ps2'

    def parse(self):
        self.wav_entries.clear()
        self.stream_entries.clear()
        self.all_wav_rows.clear()
        self.wav_tree.delete(*self.wav_tree.get_children())
        self.str_tree.delete(*self.str_tree.get_children())
        d = self.archive_data

        offs = []
        off = 0
        while True:
            off = d.find(MARKER, off)
            if off < 0:
                break
            offs.append(off)
            off += 1
        if len(offs) < 2:
            return

        self.fmt = self._detect_fmt(offs)
        is_pc = self.fmt == 'pc'

        CAT_STREAM = CAT_STREAM_PC if is_pc else CAT_STREAM_PS2
        CAT_WAV = CAT_WAV_PC if is_pc else CAT_WAV_PS2

        entries = []
        for i in range(len(offs) - 1):
            gap = offs[i + 1] - offs[i] - MARKER_LEN
            raw = d[offs[i]:offs[i] + min(gap + 4, 512)]
            etype = 'UNK'
            category = 0
            path = ''
            if gap in (HDR_GAP_PC, HDR_GAP_PS2):
                etype = 'HDR'
                category = struct.unpack_from('<I', raw, 20)[0] if len(raw) >= 24 else 0
            elif gap in ((56, 68, 72, 88, 104) if is_pc else (60, 76, 92)):
                etype = 'PATH'
                null = raw.find(b'\x00', MARKER_LEN)
                if null > MARKER_LEN:
                    path = raw[MARKER_LEN:null].decode('ascii', errors='replace')
            elif gap in ((120, 136, 152) if is_pc else (140, 156)):
                etype = 'DATA'
            entries.append({'off': offs[i], 'gap': gap, 'etype': etype, 'category': category, 'path': path, 'consumed': False})

        def mark_consumed(*idxs):
            for ix in idxs:
                if ix < len(entries):
                    entries[ix]['consumed'] = True

        n = len(entries)
        i = 0
        stream_idx = 0
        while i < n:
            e = entries[i]
            if e['etype'] != 'HDR':
                i += 1
                continue
            cat = e['category']
            if cat in CAT_STREAM and i + 2 < n:
                pe = entries[i + 1]
                de = entries[i + 2]
                if pe['etype'] == 'PATH' and de['etype'] == 'DATA':
                    path = pe['path']
                    data_off = de['off']
                    data_raw = d[data_off:data_off + de['gap'] + MARKER_LEN]
                    meta_raw = data_raw[MARKER_LEN:60] if is_pc else data_raw[MARKER_LEN:-60]
                    vol = struct.unpack_from('<f', meta_raw, 0)[0] if len(meta_raw) >= 32 else 0.0
                    pitch = struct.unpack_from('<f', meta_raw, 4)[0] if len(meta_raw) >= 32 else 0.0
                    speed = struct.unpack_from('<f', meta_raw, 8)[0] if len(meta_raw) >= 32 else 0.0
                    u1 = struct.unpack_from('<I', meta_raw, 12)[0] if len(meta_raw) >= 32 else 0
                    u2 = struct.unpack_from('<I', meta_raw, 16)[0] if len(meta_raw) >= 32 else 0
                    u3 = struct.unpack_from('<I', meta_raw, 20)[0] if len(meta_raw) >= 32 else 0
                    u4 = struct.unpack_from('<I', meta_raw, 24)[0] if len(meta_raw) >= 32 else 0
                    flags = struct.unpack_from('<I', meta_raw, 28)[0] if len(meta_raw) >= 32 else 0
                    ogg_ref = ''
                    if is_pc:
                        null = data_raw.find(b'\x00', 60)
                        if null > 60:
                            ogg_ref = data_raw[60:null].decode('ascii', errors='replace')
                    else:
                        null = data_raw.find(b'\x00', len(data_raw) - 60)
                        if null > len(data_raw) - 60:
                            ogg_ref = data_raw[len(data_raw) - 60:null].decode('ascii', errors='replace')
                    se = StreamEntry(e['off'], pe['off'], de['off'], path,
                                      vol, pitch, speed, u1, u2, u3, u4, flags,
                                      stream_idx, is_pc, ogg_ref)
                    se.data_gap = de['gap']
                    self.stream_entries.append(se)
                    self.str_tree.insert('', 'end', iid=f's{stream_idx}',
                        values=(stream_idx, path, f'{vol:.4f}', f'{pitch:.4f}', f'{speed:.4f}',
                                ogg_ref or '', f'0x{e["off"]:X}'))
                    stream_idx += 1
                    mark_consumed(i, i + 1, i + 2)
                    i += 3
                    continue
            i += 1

        def _check_wav(off, gap, hdr_off=0):
            raw = d[off:off + min(gap + 4, 512)]
            mo = -1
            sr = ch = bc = 0
            null = raw.find(b'\x00', MARKER_LEN)
            if null > MARKER_LEN:
                pa4 = ((null + 3) // 4) * 4
                mo = pa4 + 16 if pa4 % 16 == 0 else pa4 + 4
                if mo + 16 <= len(raw):
                    sr, ch, bc = read_meta_pc(raw, mo)
            if not (8000 <= sr <= 48000 and 1 <= ch <= 2 and bc > 0):
                pmm = raw.find(PCM_PS2)
                if pmm >= 68:
                    mo = pmm - 68
                    if mo + 16 <= len(raw):
                        sr, ch, bc = read_meta_pc(raw, mo)
            if 8000 <= sr <= 48000 and 1 <= ch <= 2 and bc > 0:
                path = ''
                if null > MARKER_LEN:
                    path = raw[MARKER_LEN:null].decode('ascii', errors='replace')
                pcm_start = mo + 16
                if pcm_start + bc > gap + MARKER_LEN:
                    bc = gap + MARKER_LEN - pcm_start
                wve = WAVEntry(hdr_off, off, gap, path, sr, ch, bc, pcm_start, mo)
                self.wav_entries.append(wve)
                iid = f'w{len(self.wav_entries) - 1}'
                self.all_wav_rows.append({'iid': iid, 'path': path or '(no path)',
                    'format': f'{sr}Hz {ch}ch', 'offset': f'0x{off:X}'})
                self.wav_tree.insert('', 'end', iid=iid,
                    values=(path or '(no path)', f'{sr}Hz {ch}ch', f'0x{off:X}'))

        for e in entries:
            if e['gap'] > 200:
                _check_wav(e['off'], e['gap'])

        # Also check the final entry (offs[-1]) which has no successor in entries list
        last_off = offs[-1]
        last_gap = len(d) - last_off - MARKER_LEN
        if last_gap > 200:
            _check_wav(last_off, last_gap)

    def extract_wav(self):
        sel = self.wav_tree.selection()
        if not sel:
            messagebox.showwarning('No selection', 'Select a WAV entry first')
            return
        idx = int(sel[0][1:])
        for e in self.wav_entries:
            iid = f'w{self.wav_entries.index(e)}'
            if iid == sel[0]:
                break
        else:
            return
        p = filedialog.asksaveasfilename(
            title='Save WAV as',
            initialfile=Path(e.path).name or f'wav_0x{e.wav_off:x}.wav',
            defaultextension='.wav'
        )
        if not p:
            return
        d = self.archive_data
        raw = d[e.wav_off:e.wav_off + e.gap + MARKER_LEN]
        audio = raw[e.pcm_start + AUDIO_HDR:e.pcm_start + e.bc]
        wav = build_wav(audio, e.sr, e.ch)
        with open(p, 'wb') as f:
            f.write(wav)
        messagebox.showinfo('Extracted', f'{len(wav)} bytes -> {Path(p).name}')

    def replace_wav(self):
        sel = self.wav_tree.selection()
        if not sel:
            messagebox.showwarning('No selection', 'Select a WAV entry first')
            return
        idx = int(sel[0][1:])
        for e in self.wav_entries:
            iid = f'w{self.wav_entries.index(e)}'
            if iid == sel[0]:
                break
        else:
            return
        p = filedialog.askopenfilename(title='Open replacement WAV', filetypes=[('WAV files', '*.wav')])
        if not p:
            return
        with open(p, 'rb') as f:
            wd = f.read()
        pcm = wav_to_pcm(wd)
        if pcm is None:
            messagebox.showerror('Error', 'Could not extract PCM from WAV')
            return
        if len(pcm) == 0:
            messagebox.showerror('Error', 'WAV has no PCM data')
            return
        sr, ch, bps = read_wav_fmt(wd)
        d = self.archive_data
        pcm_start_abs = e.wav_off + e.pcm_start
        max_audio = e.bc - AUDIO_HDR
        header_end = pcm_start_abs + AUDIO_HDR
        if len(pcm) > max_audio:
            pcm, sr, ch = downsample_pcm(pcm, sr, ch, max_audio)
        write_len = min(len(pcm), max_audio)
        d[header_end:header_end + write_len] = pcm[:write_len]
        if write_len < max_audio:
            d[header_end + write_len:pcm_start_abs + e.bc] = b'\x00' * (max_audio - write_len)
        if e.mo >= 0:
            struct.pack_into('<I', d, e.wav_off + e.mo, sr)
            struct.pack_into('<H', d, e.wav_off + e.mo + 4, ch)
        messagebox.showinfo('Replaced', f'Injected {write_len} bytes audio (sr={sr}Hz, ch={ch})')

    def edit_stream(self):
        sel = self.str_tree.selection()
        if not sel:
            messagebox.showwarning('No selection', 'Select a stream entry first')
            return
        idx = int(sel[0][1:])
        e = self.stream_entries[idx]
        dlg = EditStreamDialog(self.win, e)
        self.win.wait_window(dlg)
        if dlg.result is None:
            return
        r = dlg.result
        d = self.archive_data
        if e.is_pc:
            meta_off = e.data_off + MARKER_LEN
        else:
            meta_off = e.data_off + MARKER_LEN
        struct.pack_into('<f', d, meta_off, r['volume'])
        struct.pack_into('<f', d, meta_off + 4, r['pitch'])
        struct.pack_into('<f', d, meta_off + 8, r['speed'])
        new_path = r['path'].encode('ascii') + b'\x00'
        path_off = e.path_off + MARKER_LEN
        path_raw = d[e.path_off:e.path_off + 512]
        old_null = path_raw.find(b'\x00', MARKER_LEN)
        if old_null > MARKER_LEN:
            path_end = e.path_off + old_null
            old_path_len = old_null - MARKER_LEN
            new_path_len = len(new_path)
            if new_path_len <= old_path_len + 4:
                d[path_off:path_off + new_path_len] = new_path
                if new_path_len < old_path_len + 4:
                    d[path_off + new_path_len:path_end + 1] = b'\x00' * (old_path_len + 1 - new_path_len)
            else:
                d[path_off:path_off + old_path_len + 4] = new_path[:old_path_len + 4]
        self.str_tree.item(sel[0], values=(e.stream_idx, r['path'],
            f'{r["volume"]:.4f}', f'{r["pitch"]:.4f}', f'{r["speed"]:.4f}',
            e.ogg_ref or '', f'0x{e.hdr_off:X}'))
        e.path = r['path']
        e.volume = r['volume']
        e.pitch = r['pitch']
        e.speed = r['speed']

    def edit_ogg(self):
        sel = self.str_tree.selection()
        if not sel:
            messagebox.showwarning('No selection', 'Select a stream entry first')
            return
        idx = int(sel[0][1:])
        e = self.stream_entries[idx]
        if not e.ogg_ref:
            messagebox.showinfo('No OGG', 'No OGG reference found for this stream')
            return
        from tkinter.simpledialog import askstring
        new_ref = askstring('Edit OGG Reference',
                           f'Current: {e.ogg_ref}\n\nEnter new OGG path:',
                           initialvalue=e.ogg_ref)
        if new_ref is None or new_ref == e.ogg_ref:
            return
        d = self.archive_data
        if e.is_pc:
            ref_off = e.data_off + 60
        else:
            ref_off = e.data_off + e.data_gap - 56
        old_null = d.find(b'\x00', ref_off)
        if old_null < ref_off:
            return
        new_bytes = new_ref.encode('ascii') + b'\x00'
        old_len = old_null - ref_off
        if len(new_bytes) <= old_len + 4:
            d[ref_off:ref_off + len(new_bytes)] = new_bytes
            if len(new_bytes) < old_len + 4:
                d[ref_off + len(new_bytes):ref_off + old_len + 4] = b'\x00' * (old_len + 4 - len(new_bytes))
        else:
            d[ref_off:ref_off + old_len + 4] = new_bytes[:old_len + 4]
        e.ogg_ref = new_ref
        self.str_tree.item(sel[0], values=(e.stream_idx, e.path,
            f'{e.volume:.4f}', f'{e.pitch:.4f}', f'{e.speed:.4f}', new_ref, f'0x{e.hdr_off:X}'))
        messagebox.showinfo('Done', f'Updated OGG ref to:\n{new_ref}')

if __name__ == '__main__':
    ODUKGUI()