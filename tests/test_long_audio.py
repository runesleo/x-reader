"""Long-audio source verification, resumable byte chunks, temporal ASR and fail-closed receipts."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from x_reader import long_audio as long
from x_reader.cli import main as cli_main
from x_reader.evidence import build_receipt, classify_payload
from x_reader.fetchers.podcast import fetch_podcast
from x_reader.reader import UniversalReader
from x_reader.schema import from_podcast

EPISODE = "https://www.xiaoyuzhoufm.com/episode/6ab301386ff98691134a1b26"
CDN = "https://media.xyzcdn.net/public/long-sample.mp3"
TEXT = "第一段中文文字\n第二段转录文字\n"
def sha(b):
    return hashlib.sha256(b).hexdigest()
def canonical(x):
    return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()

def proof():
    source_size=1024*1024+1234
    chunk_data={
      "0":{"start":0,"end":1024*1024-1,"sha256":"a"*64},
      "1":{"start":1024*1024,"end":source_size-1,"sha256":"b"*64},
    }
    chunk_sec=[
      {"index":0,"start_seconds":0.0,"end_seconds":60.0,
       "sample_frames":960000,"pcm_sha256":"c"*64,
       "transcript_sha256":sha("第一段中文文字".encode()),
       "transcript_chars":len("第一段中文文字"),"asr_segment_count":3},
      {"index":1,"start_seconds":60.0,"end_seconds":120.0,
       "sample_frames":960000,"pcm_sha256":"d"*64,
       "transcript_sha256":sha("第二段转录文字".encode()),
       "transcript_chars":len("第二段转录文字"),"asr_segment_count":2},
    ]
    return {
      "full_transcript":"第一段中文文字\n第二段转录文字",
      "has_transcript":True,"transcript_coverage":"full",
      "transcription_method":"local_whisper_tiny_cpu",
      "coverage_basis":"verified_chunk_manifest_and_contiguous_pcm_asr",
      "verified_complete_bytes":True,
      "audio_bytes":source_size,"media_sha256":"e"*64,
      "media_url_sha256":"f"*64,"transcript_sha256":sha("第一段中文文字\n第二段转录文字".encode()),
      "media_duration_seconds":120.0,"decoded_duration_seconds":120.0,
      "processed_seconds":120.0,"coverage_ratio":1.0,
      "chunk_count":2,"chunks_manifest":chunk_data,
      "chunk_manifest_sha256":sha(canonical(chunk_data)),
      "segment_count":2,"segments_manifest":chunk_sec,
      "segments_manifest_sha256":sha(canonical(chunk_sec)),
      "coverage_intervals":[
          {"start_seconds":0.0,"end_seconds":60.0},
          {"start_seconds":60.0,"end_seconds":120.0},
      ],
      "asr_segments":5,
      "reused_source_chunks":0,
      "reused_asr_segments":0,
    }

class ChunkSourceContractTests(unittest.TestCase):
    def response(self, status=206, start=0, end=1023, total=8192,
                 etag='"same-source-etag"', mime="audio/mpeg"):
        r=MagicMock()
        r.status=status
        r.headers={
          "Content-Type":mime,"Content-Range":f"bytes {start}-{end}/{total}",
          "Content-Length":str(end-start+1),"ETag":etag
        }
        return r

    def test_strict_206_validator_rejects_size_type_etag_and_range(self):
        for kwargs in (
          {"status":200},{"status":302},{"mime":"text/html"},
          {"start":1},{"end":100},{"total":65*1024*1024},
          {"etag":""},{"etag":'W/"weak"'}
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(RuntimeError):
                    long._checked_response(self.response(**kwargs),0,1023)
        self.assertEqual(
          long._checked_response(self.response(),0,1023),(8192,'"same-source-etag"')
        )
        with self.assertRaisesRegex(RuntimeError,"changed ETag"):
            long._checked_response(self.response(),0,1023,expected_etag='"other"')

    def test_exact_chunk_fetch_and_three_attempt_fail_closed(self):
        response=self.response(end=1023,total=8192)
        response.read.return_value=b"x"*1024
        pool=MagicMock()
        with patch("x_reader.long_audio._range_response",
                   return_value=(pool,response,"203.0.113.12")):
            raw=long._read_chunk(CDN,0,1023,8192,'"same-source-etag"')
        self.assertEqual(len(raw),1024)
        self.assertEqual(raw,b"x"*1024)
        failure=self.response(end=1023,total=8192)
        failure.read.side_effect=[b"z"*100,b""]*3
        with patch("x_reader.long_audio._range_response",
                   return_value=(MagicMock(),failure,"203.0.113.12")) as call:
            with patch("x_reader.long_audio.time.sleep"):
                with self.assertRaisesRegex(RuntimeError,"after 3 attempts"):
                    long._read_chunk(CDN,0,1023,4096,'"same-source-etag"')
        self.assertEqual(call.call_count,3)

    def test_real_checkpoint_resume_and_modified_chunk_redownload(self):
        # Four chunks, interrupt after two. Resume must reuse both verified.
        raw=b"A"*8192+b"B"*8192+b"C"*8192+b"D"*10
        calls=[]
        def get_range(_url,start,end,_size,_etag):
            calls.append((start,end))
            return raw[start:end+1]
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder)
            with patch.object(long,"DOWNLOAD_CHUNK_BYTES",8192):
                with patch.object(long,"_public_episode_url",return_value=EPISODE):
                    with patch.object(long,"_validated_media_url",return_value=CDN):
                        with patch.object(long,"_probe",return_value=(len(raw),'"one"')):
                            with patch.object(long,"_read_chunk",side_effect=get_range):
                                with self.assertRaisesRegex(RuntimeError,"interrupted"):
                                    long.fetch_chunked_source(
                                        EPISODE,CDN,job,max_new_chunks=2
                                    )
                                self.assertEqual(len(calls),2)
                                meta=long.fetch_chunked_source(EPISODE,CDN,job)
                                self.assertEqual(meta["new_chunks"],2)
                                self.assertEqual(meta["reused_chunks"],2)
                                self.assertEqual(meta["audio_bytes"],len(raw))
                                self.assertEqual(meta["media_sha256"],sha(raw))
                                self.assertEqual(len(calls),4)
                                # Corrupt an existing chunk with same length.
                                (job/"chunk-0001.bin").write_bytes(b"!"*8192)
                                again=long.fetch_chunked_source(EPISODE,CDN,job)
                                self.assertEqual(again["new_chunks"],1)
                                self.assertEqual(again["reused_chunks"],3)
                                self.assertEqual(len(calls),5)
                                self.assertEqual(again["media_sha256"],sha(raw))
                                # Source revision cannot silently reuse chunks.
                                with patch.object(long,"_probe",return_value=(len(raw),'"new"')):
                                    with self.assertRaisesRegex(RuntimeError,"different source version"):
                                        long.fetch_chunked_source(EPISODE,CDN,job)
            self.assertNotIn(CDN,(job/"chunks.json").read_text())
            self.assertTrue((job/"chunks.json").stat().st_mode & 0o077 == 0)

    def test_existing_shared_cache_path_is_refused_without_chmod(self):
        import stat
        with tempfile.TemporaryDirectory() as folder:
            shared = Path(folder) / "shared"
            shared.mkdir(mode=0o755)
            shared.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "not private"):
                long._private_directory(shared)
            self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o755)

    def test_oversized_checkpoint_json_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "oversized.json"
            path.write_bytes(b"{" + b"x" * (1024 * 1024 + 2))
            self.assertIsNone(long._read_json(path))

    def test_checkpoint_rejects_symlink_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            target=base/"real"
            target.mkdir()
            alias=base/"alias"
            alias.symlink_to(target,target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError,"Unsafe"):
                long._private_directory(alias)


class PCMAndASRCheckpointTests(unittest.TestCase):
    def test_three_decoded_intervals_are_contiguous(self):
        pcm=b"\x00\x00"*(3*16000)
        class MockProcess:
            def __init__(self,*args,**kwargs):
                self.stdout=io.BytesIO(pcm)
            def poll(self):
                return 0
            def wait(self,timeout=None):
                return 0
            def kill(self):
                raise AssertionError("should not kill completed process")
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(long,"PCM_SEGMENT_SECONDS",1):
                with patch("x_reader.long_audio.subprocess.Popen",MockProcess):
                    records=list(long._decode_segments(Path(folder)/"audio",folder,3))
        self.assertEqual(len(records),3)
        self.assertEqual([r["index"] for r in records],[0,1,2])
        self.assertEqual([r["start_seconds"] for r in records],[0.0,1.0,2.0])
        self.assertEqual([r["end_seconds"] for r in records],[1.0,2.0,3.0])

    def test_whisper_slight_end_timestamp_overrun_is_recorded_not_silent(self):
        # Reproduces real third 60-second PCM segment: last ASR span
        # 57.36 .. 61.36 although the decoded audio ends exactly at 60.
        model = MagicMock()
        model.transcribe.return_value = (
            iter([
                types.SimpleNamespace(start=0.0, end=25.0, text="first part"),
                types.SimpleNamespace(start=57.36, end=61.36, text="last part"),
            ]),
            types.SimpleNamespace(language="zh"),
        )
        segment = {
            "index": 2, "start_seconds": 120.0, "end_seconds": 180.0,
            "sample_frames": 960000, "pcm_sha256": "b"*64,
            "wav_path": "/tmp/mock-not-read",
        }
        result = long._transcribe_segment(model, segment)
        self.assertEqual(result["asr_segment_count"], 2)
        self.assertEqual(result["asr_boundary_adjustments"], 1)
        self.assertAlmostEqual(result["max_boundary_overrun_seconds"], 1.36)
        self.assertEqual(result["transcript"], "first part last part")

    def test_whisper_gross_or_out_of_window_timestamps_fail_closed(self):
        segment = {
            "index": 2, "start_seconds": 120.0, "end_seconds": 180.0,
            "sample_frames": 960000, "pcm_sha256": "b"*64,
            "wav_path": "/tmp/mock-not-read",
        }
        for start, end in ((57, 70), (60, 61), (20, 61.36),
                           (57.5, 62.6), (57, float("nan"))):
            model = MagicMock()
            model.transcribe.return_value = (
                iter([types.SimpleNamespace(start=start, end=end, text="fake")]),
                types.SimpleNamespace(language="zh"),
            )
            with self.subTest(start=start, end=end):
                with self.assertRaisesRegex(RuntimeError, "ASR timestamps"):
                    long._transcribe_segment(model, segment)

    def test_asr_checkpoint_only_reused_for_same_pcm_and_transcript_hash(self):
        transcript="正确识别文本"
        model=MagicMock()
        model.transcribe.return_value=(
            iter([types.SimpleNamespace(start=0,end=1,text=transcript)]),
            types.SimpleNamespace(language="zh"),
        )
        seg={"index":0,"start_seconds":0.0,"end_seconds":1.0,
             "sample_frames":16000,"pcm_sha256":"a"*64,
             "wav_path":"/tmp/fake-path-unused"}
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder)
            a,cached=long._segment_checkpoint(job,"b"*64,seg,model)
            self.assertFalse(cached)
            b,cached=long._segment_checkpoint(job,"b"*64,seg,model)
            self.assertTrue(cached)
            self.assertEqual(model.transcribe.call_count,1)
            saved=job/"asr-0000.json"
            corrupted=json.loads(saved.read_text())
            corrupted["transcript"]="tampered"
            long._write_private_json(saved,corrupted)
            model.transcribe.return_value=(
              iter([types.SimpleNamespace(start=0,end=1,text=transcript)]),
              types.SimpleNamespace(language="zh"),
            )
            c,cached=long._segment_checkpoint(job,"b"*64,seg,model)
            self.assertFalse(cached)
            self.assertEqual(model.transcribe.call_count,2)
            changed=dict(seg,pcm_sha256="c"*64)
            model.transcribe.return_value=(
              iter([types.SimpleNamespace(start=0,end=1,text=transcript)]),
              types.SimpleNamespace(language="zh"),
            )
            d,cached=long._segment_checkpoint(job,"b"*64,changed,model)
            self.assertFalse(cached)
            self.assertEqual(model.transcribe.call_count,3)
            self.assertNotIn(CDN,saved.read_text())


class LongAudioEvidenceTests(unittest.TestCase):
    def test_verified_manifest_full_audio_is_media_complete(self):
        data=from_podcast(dict(
            proof(), title="Two minute episode",url=EPISODE,
            description="This is a public podcast description. "*10,
        )).to_dict()
        receipt=build_receipt(data)
        self.assertEqual(receipt["status"],"PASS")
        self.assertEqual(receipt["reason_code"],"spoken_media_complete_verified")
        self.assertEqual(receipt["segment_count"],2)
        self.assertEqual(receipt["chunk_count"],2)
        self.assertNotIn("chunks_manifest",receipt)
        self.assertNotIn("segments_manifest",receipt)
        self.assertNotIn("full_transcript",receipt)
        self.assertNotIn(CDN,json.dumps(receipt))

    def test_unbounded_asr_boundary_adjustments_cannot_become_pass(self):
        p = from_podcast(dict(
            proof(), title="Episode", url=EPISODE, description="Notes " * 20
        )).to_dict()
        self.assertEqual(build_receipt(p)["status"], "PASS")
        cases = (
            {"asr_boundary_adjustments": 9, "max_boundary_overrun_seconds": 1.0},
            {"asr_boundary_adjustments": 1, "max_boundary_overrun_seconds": 4.0},
        )
        for edits in cases:
            changed = json.loads(json.dumps(p))
            changed["extra"]["segments_manifest"][0].update(edits)
            changed["extra"]["segments_manifest_sha256"] = sha(
                canonical(changed["extra"]["segments_manifest"])
            )
            changed["extra"]["asr_boundary_adjustments"] = edits["asr_boundary_adjustments"]
            changed["extra"]["max_boundary_overrun_seconds"] = edits["max_boundary_overrun_seconds"]
            self.assertEqual(build_receipt(changed)["status"], "PARTIAL")

    def test_tampered_manifests_gap_hash_and_audio_duration_are_partial(self):
        baseline=from_podcast(dict(proof(),title="Episode",
                        description="Show notes "*20,url=EPISODE)).to_dict()
        cases={
          "chunk_count": lambda x:x["extra"].update(chunk_count=1),
          "source_size": lambda x:x["extra"].update(audio_bytes=1),
          "chunk_hash": lambda x:x["extra"]["chunks_manifest"]["0"].update(sha256="bad"),
          "chunk_offset": lambda x:x["extra"]["chunks_manifest"]["1"].update(start=2),
          "seg_gap":lambda x:x["extra"]["segments_manifest"][1].update(start_seconds=65.0),
          "interval_gap":lambda x:x["extra"]["coverage_intervals"][1].update(start_seconds=61.0),
          "segment_hash":lambda x:x["extra"]["segments_manifest"][0].update(transcript_sha256="0"*64),
          "segment_pcm_hash":lambda x:x["extra"]["segments_manifest"][0].update(pcm_sha256="bad"),
          "asr_missing": lambda x:x["extra"].update(asr_segments=0),
          "duration_short":lambda x:x["extra"].update(decoded_duration_seconds=60.0),
          "over_max":lambda x:x["extra"].update(media_duration_seconds=5401),
          "ratio_short":lambda x:x["extra"].update(coverage_ratio=0.5),
          "text_spoof":lambda x:x.update(content="Tampered transcription"),
          "no_bytes":lambda x:x["extra"].update(verified_complete_bytes=False),
        }
        for name,attack in cases.items():
            p=json.loads(json.dumps(baseline))
            attack(p)
            with self.subTest(name=name):
                receipt=build_receipt(p)
                self.assertEqual(receipt["status"],"PARTIAL")
                self.assertEqual(receipt["reason_code"],"full_media_proof_unverified")

    def test_multiline_coverage_handles_empty_final_segment_without_fake_complete(self):
        data=dict(proof(),full_transcript="第一段中文文字\n第二段转录文字\n")
        segments=data["segments_manifest"]+[{
          "index":2,"start_seconds":120.0,"end_seconds":130.0,
          "sample_frames":160000,"pcm_sha256":"a"*64,
          "transcript_sha256":sha(b""),"transcript_chars":0,"asr_segment_count":0
        }]
        data.update(segments_manifest=segments,segment_count=3,
                    segments_manifest_sha256=sha(canonical(segments)),
                    coverage_intervals=[
                      {"start_seconds":0.0,"end_seconds":60.0},
                      {"start_seconds":60.0,"end_seconds":120.0},
                      {"start_seconds":120.0,"end_seconds":130.0},
                    ],media_duration_seconds=130.0,decoded_duration_seconds=130.0,
                    processed_seconds=130.0,transcript_sha256=sha(data["full_transcript"].encode()))
        item=from_podcast(dict(data,url=EPISODE,title="Episode")).to_dict()
        self.assertEqual(item["content"],data["full_transcript"])
        self.assertEqual(build_receipt(item)["status"],"PASS")


class LongAudioRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_invalid_mixed_media_options_are_rejected(self):
        for kwargs in (
            {"full_long_audio":True,"full_short_audio":True},
            {"full_long_audio":True,"media_preview_seconds":12},
        ):
            with self.assertRaisesRegex(ValueError,"cannot be enabled together"):
                UniversalReader(**kwargs)

    async def test_reader_routes_long_audio_without_triggering_regular_preview(self):
        with patch("x_reader.fetchers.podcast.fetch_podcast",new_callable=AsyncMock,
                   return_value={"title":"Episode","description":"Show notes "*20,"url":EPISODE}) as call:
            item=await UniversalReader(full_long_audio=True)._fetch("podcast",EPISODE)
        call.assert_awaited_once_with(EPISODE,full_long_audio=True)
        self.assertEqual(build_receipt(item.to_dict())["status"],"PARTIAL")

    async def test_full_long_rejects_batch_and_non_podcast(self):
        with self.assertRaisesRegex(ValueError,"exactly one"):
            await UniversalReader(full_long_audio=True).read_batch([EPISODE,EPISODE])
        with patch("x_reader.reader.validate_url",return_value=None):
            with self.assertRaisesRegex(ValueError,"podcast episodes only"):
                await UniversalReader(full_long_audio=True).read("https://example.com")

    def test_cli_accepts_one_explicit_long_audio_case_only(self):
        with patch.object(sys,"argv",["x-reader",EPISODE,"--media-full-long","--json"]):
            with patch("x_reader.cli.cmd_fetch") as fetch:
                cli_main()
        fetch.assert_called_once_with([EPISODE],json_output=True,full_long_audio=True)
        with patch.object(sys,"argv",["x-reader",EPISODE,"--media-full-long",
                                      "--media-full-short","--json"]):
            with self.assertRaises(SystemExit) as error:
                cli_main()
        self.assertEqual(error.exception.code,2)

    async def test_source_download_failure_keeps_metadata_partial(self):
        page={"title":"Long public episode","content":"Show notes "*20,"url":EPISODE}
        with patch("x_reader.fetchers.podcast.fetch_direct_html",return_value=page):
            with patch("x_reader.long_audio.transcribe_long",
                       side_effect=RuntimeError("incomplete-chunks")):
                output=await fetch_podcast(EPISODE,full_long_audio=True)
        self.assertEqual(output["full_error"],"RuntimeError")
        self.assertEqual(build_receipt(from_podcast(output).to_dict())["status"],"PARTIAL")


if __name__ == "__main__":
    unittest.main()
