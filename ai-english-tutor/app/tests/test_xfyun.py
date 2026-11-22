import io
import unittest
import wave

from backend.xfyun_ise import parse_result, wav_to_pcm16k


class XfyunParsingTest(unittest.TestCase):
    def test_five_point_scores_are_normalized(self):
        xml = """<xml_result><read_sentence lan="en"><rec_paper>
        <read_chapter accuracy_score="4.5" fluency_score="4.0" integrity_score="5.0"
          total_score="4.5" content="I like reading." word_count="3">
          <word content="I" total_score="5.0" dp_message="0"/>
          <word content="like" total_score="3.2" dp_message="0"/>
          <word content="reading" total_score="0" dp_message="1"/>
        </read_chapter></rec_paper></read_sentence></xml_result>"""
        result = parse_result(xml, "I like reading.")
        self.assertFalse(result["demo"])
        self.assertEqual(result["overall_score"], 90)
        self.assertEqual(result["accuracy"], 90)
        self.assertEqual(result["completeness"], 100)
        self.assertEqual(result["focus_words"], ["like", "reading"])

    def test_wav_is_checked_and_kept_mono(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(16000)
            target.writeframes(b"\x00\x01" * 8000)
        info = wav_to_pcm16k(buffer.getvalue())
        self.assertEqual(len(info.pcm), 16000)
        self.assertAlmostEqual(info.duration_seconds, 0.5, places=2)

    def test_rejects_non_wav(self):
        with self.assertRaises(ValueError):
            wav_to_pcm16k(b"not a wav")


if __name__ == "__main__":
    unittest.main()
