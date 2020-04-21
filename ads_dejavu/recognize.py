import ads_dejavu.fingerprint as fingerprint
import ads_dejavu.decoder as decoder
import numpy as np
import pyaudio
import time
from resampy import resample


class BaseRecognizer(object):

    def __init__(self, dejavu):
        self.dejavu = dejavu
        self.Fs = fingerprint.DEFAULT_FS

    def _recognize(self, *data):
        matches = []
        total_hashes = 0
        audio_len = len(data[-1]) / self.Fs
        for d in data:
            extracted_matches = self.dejavu.find_matches(d, Fs=self.Fs)
            total_hashes += extracted_matches[1]
            matches.extend(extracted_matches[0])
        return self.dejavu.align_matches(matches, total_hashes, audio_len)

    def recognize(self, *args, **kwargs):
        pass  # base class does nothing


class FileRecognizer(BaseRecognizer):
    def __init__(self, dejavu):
        super(FileRecognizer, self).__init__(dejavu)

    def recognize_file(self, filename):
        frames, self.Fs, file_hash, audio_length = decoder.read(filename, self.dejavu.limit)
        if decoder.RESAMPLE:
            frames = resample(np.array(frames, dtype=np.int16), self.Fs, fingerprint.DEFAULT_FS, axis=-1)
            self.Fs = fingerprint.DEFAULT_FS
        t = time.time()
        match = self._recognize(*frames)
        t = time.time() - t

        if match:
            match['match_time'] = t

        return match

    def recognize(self, filename):
        return self.recognize_file(filename)


class MicrophoneRecognizer(BaseRecognizer):
    default_chunksize   = 8192
    default_format      = pyaudio.paInt16
    default_channels    = 1 if decoder.CONVERT_TO_MONO else 2
    default_samplerate  = fingerprint.DEFAULT_FS

    def __init__(self, dejavu):
        super(MicrophoneRecognizer, self).__init__(dejavu)
        self.audio = pyaudio.PyAudio()
        self.stream = None
        self.data = []
        self.channels = MicrophoneRecognizer.default_channels
        self.chunksize = MicrophoneRecognizer.default_chunksize
        self.samplerate = MicrophoneRecognizer.default_samplerate
        self.recorded = False

    def start_recording(self, channels=default_channels,
                        samplerate=default_samplerate,
                        chunksize=default_chunksize):
        self.chunksize = chunksize
        self.channels = channels
        self.recorded = False
        self.samplerate = samplerate

        if self.stream:
            self.stream.stop_stream()
            self.stream.close()

        self.stream = self.audio.open(
            format=self.default_format,
            channels=channels,
            rate=samplerate,
            input=True,
            frames_per_buffer=chunksize,
        )

        self.data = [[] for i in range(channels)]

    def process_recording(self):
        data = self.stream.read(self.chunksize)
        nums = np.fromstring(data, np.int16)
        for c in range(self.channels):
            self.data[c].extend(nums[c::self.channels])

    def stop_recording(self):
        self.stream.stop_stream()
        self.stream.close()
        self.stream = None
        self.recorded = True

    def recognize_recording(self):
        if not self.recorded:
            raise NoRecordingError("Recording was not complete/begun")
        return self._recognize(*self.data)

    def get_recorded_time(self):
        return len(self.data[0]) / self.rate

    def recognize(self, seconds=10):
        self.start_recording()
        for i in range(0, int(self.samplerate / self.chunksize
                              * seconds)):
            self.process_recording()
        self.stop_recording()
        return self.recognize_recording()


class NumpyArrayRecognizer(BaseRecognizer):
    def __init__(self, dejavu):
        super(NumpyArrayRecognizer, self).__init__(dejavu)

    def recognize_array(self, frames, sr):
        t = time.time()
        if decoder.CONVERT_TO_MONO:
            frames = np.array([np.mean(frames, axis=0)], dtype=frames.dtype)
        if decoder.RESAMPLE and sr != self.Fs and len(frames[-1]) > 0:
            frames = resample(frames, sr, fingerprint.DEFAULT_FS, axis=-1)
            self.Fs = fingerprint.DEFAULT_FS
        if decoder.NORMALIZE and len(frames[-1]) > 0:
            gain = (-np.iinfo(frames.dtype).min) / np.max(np.abs(frames))
            frames = np.array(frames * gain, dtype=frames.dtype)
        match = self._recognize(*frames)
        t = time.time() - t
        if match:
            match['match_time'] = t
        return match

    def recognize(self, data, sr=44100):
        return self.recognize_array(data, sr)


class NoRecordingError(Exception):
    pass
