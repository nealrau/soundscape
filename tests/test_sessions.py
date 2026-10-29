"""Continuous capture lifecycle and snapshot regression tests (no microphone required)."""
import json
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from soundscape_mcp.sessions import Session
from soundscape_mcp import tools
from soundscape_mcp.practice import create_card


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def wav(self, name, value, seconds=2):
        import struct
        path = self.root / name
        with wave.open(str(path), 'wb') as w:
            w.setparams((1, 2, 100, 0, 'NONE', 'not compressed'))
            w.writeframes(struct.pack('<h', value) * (seconds * 100))
        return path

    def session(self):
        s = Session.__new__(Session)
        s.audio_t = 4
        s._files_lock = threading.Lock()
        s.segments = [(self.wav('one.wav', 100), 0), (self.wav('two.wav', 200), 2)]
        return s

    def test_snapshot_spans_rotation(self):
        import struct
        s = self.session()
        path = self.root / 'snapshot.wav'
        self.assertEqual(s.snapshot(path, last_seconds=3), (1, 4))
        with wave.open(str(path)) as w:
            values = struct.unpack('<300h', w.readframes(300))
        self.assertEqual(values[:100], (100,) * 100)
        self.assertEqual(values[100:], (200,) * 200)

    def test_snapshot_ignores_expired_segments(self):
        s = self.session(); s.segments = s.segments[1:]
        self.assertEqual(s.snapshot(self.root / 'clip.wav', last_seconds=600), (2, 4))

    def test_snapshot_empty_has_actionable_error(self):
        s = self.session(); s.audio_t = 0
        with self.assertRaisesRegex(ValueError, 'No audio'):
            s.snapshot(self.root / 'empty.wav')

    def test_continuous_passes_no_duration_and_does_not_stop(self):
        session = SimpleNamespace(id='test', source='output', companion=None, companion_error=None)
        with patch.object(tools.REGISTRY, 'active_ids', return_value=[]), patch.object(tools, '_start_session', return_value=session) as start:
            text = tools.t_start({'source': 'output', 'continuous': True}, None)
            self.assertEqual(start.call_args.args[3], 0)
            self.assertIn('Listening ON', text)

    def test_reuses_matching_continuous_session(self):
        session = SimpleNamespace(max_seconds=0, source='output', requested_device=None, companion=None,
                                  describe=lambda: 'active')
        with patch.object(tools.REGISTRY, 'active_ids', return_value=['test']), patch.object(tools.REGISTRY, 'get', return_value=session), patch.object(tools, '_start_session') as start:
            self.assertIn('Already listening', tools.t_start({'source':'output', 'continuous':True}, None))
            start.assert_not_called()

    def test_off_does_not_wait_for_analysis(self):
        from unittest.mock import Mock
        s = Mock(running=True)
        with patch.object(tools.REGISTRY, 'get', return_value=s), patch.object(tools, '_analyze_group') as analyze:
            s.describe.return_value = 'stopped'
            self.assertIn('OFF', tools.t_stop({'analyze':False}, None))
            s.stop.assert_called_once()
            analyze.assert_not_called()

    def test_stream_cursor_advances_only_after_success(self):
        from unittest.mock import Mock
        s = Mock(audio_t=10, stream_cursor=0, stream_lock=threading.Lock(), running=True)
        with patch.object(tools.REGISTRY, 'get', return_value=s), patch.object(tools, '_select_source', return_value=s), patch.object(tools, '_analyze_session', side_effect=tools.ToolError('failed')):
            with self.assertRaises(tools.ToolError): tools.t_stream({}, None)
            self.assertEqual(s.stream_cursor, 0)
        with patch.object(tools.REGISTRY, 'get', return_value=s), patch.object(tools, '_select_source', return_value=s), patch.object(tools, '_analyze_session', return_value='report'):
            self.assertIn('Capture ON', tools.t_stream({}, None))
            self.assertEqual(s.stream_cursor, 10)

    def test_stream_uses_selected_source_clock(self):
        from unittest.mock import Mock
        primary = Mock(audio_t=1000, running=True)
        output = Mock(audio_t=10, source="output", stream_cursor=5, stream_lock=threading.Lock())
        with patch.object(tools.REGISTRY, 'get', return_value=primary), patch.object(tools, '_select_source', return_value=output), patch.object(tools, '_analyze_session', return_value='report') as analyze:
            tools.t_stream({'source':'output'}, None)
            self.assertEqual(output.stream_cursor, 10)
            self.assertEqual(analyze.call_args.kwargs['start'], 5)

    def test_replay_uses_analysed_clip_after_new_audio(self):
        s = self.session()
        s.id = 'test'; s.proc = SimpleNamespace(poll=lambda: None)
        s.last_analysis_path = self.wav('analysed.wav', 123)
        s.last_analysis_window = (0, 2)
        with patch.object(tools.REGISTRY, 'get', return_value=s), patch.object(tools, '_select_source', return_value=s), patch.object(tools.env, 'SESSIONS', self.root):
            result = tools.t_replay({'play':False}, None)
        replay = next(self.root.glob('test-replay-*.wav'))
        with wave.open(str(replay)) as wav:
            self.assertEqual(wav.readframes(1), b'\x7b\x00')
        self.assertIn('0.0–2.0s', result)

    def test_off_can_delete_retained_audio_without_analysis(self):
        from unittest.mock import Mock
        s = self.session()
        s.last_analysis_path = self.wav('analysed.wav', 123)
        s.proc = SimpleNamespace(poll=lambda: None); s.companion = None
        s.stop = Mock(); s.describe = Mock(return_value='stopped')
        with patch.object(tools.REGISTRY, 'get', return_value=s):
            tools.t_stop({'analyze':False, 'keep_audio':False}, None)
        self.assertFalse(any(self.root.glob('*.wav')))

    def test_invalid_practice_answer(self):
        with self.assertRaisesRegex(ValueError, 'zero-based'):
            create_card(self.root, {'question':'Cadence?', 'choices':['PAC','HC'], 'answer':2})

    def test_new_tool_schemas(self):
        for name in ('stream', 'replay', 'practice_card'):
            self.assertIn(name, tools.BY_NAME)
            json.dumps(tools.BY_NAME[name]['inputSchema'])

if __name__ == '__main__': unittest.main()
