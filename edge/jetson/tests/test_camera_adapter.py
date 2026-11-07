"""No camera, model downloads, lock adapters or hardware are used by these tests."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib import error, request

from edge.jetson.airlock_camera import (
    DatabaseClient, Metrics, OpenCVDetector, SequenceStore, ZoneLatch, acquire_process_lock,
    bind_requires_auth, box_in_roi, identifier, metrics_text, parse_roi, read_secret,
    serve_metrics, sql_rows,
)


class RoiTests(unittest.TestCase):
    def test_roi_valid_and_bounded(self):
        self.assertEqual(parse_roi('0,0,1,1'),(0,0,1,1))
        for invalid in ['0,0,0,1','0.9,0,0.2,1','-1,0,1,1','0,0,inf,1','0,0,nan,1','a,b,c,d','0,0,1']:
            with self.subTest(invalid=invalid),self.assertRaises(ValueError): parse_roi(invalid)

    def test_bbox_center_has_inclusive_roi_boundaries(self):
        roi=(.25,.25,.5,.5)
        self.assertTrue(box_in_roi((20,20,10,10),100,100,roi))
        self.assertTrue(box_in_roi((70,70,10,10),100,100,roi))
        self.assertFalse(box_in_roi((0,0,10,10),100,100,roi))
        self.assertFalse(box_in_roi((0,0,10,10),0,100,roi))
        self.assertFalse(box_in_roi((20,20,0,10),100,100,roi))

    def test_rejects_sql_and_path_identifiers(self):
        for invalid in ["a'; SELECT x",'../private','a b','', 'x'*129]:
            with self.subTest(invalid=invalid),self.assertRaises(ValueError): identifier(invalid)


class LatchTests(unittest.TestCase):
    def test_debounces_enter_and_clear(self):
        latch=ZoneLatch(3,2)
        self.assertIsNone(latch.observe(True,1000))
        self.assertIsNone(latch.observe(False,1100))
        self.assertIsNone(latch.observe(True,1200))
        self.assertIsNone(latch.observe(True,1300))
        self.assertEqual(latch.observe(True,1400),'person_in_zone')
        self.assertIsNone(latch.observe(False,1500))
        self.assertEqual(latch.observe(False,1600),'person_cleared')
        self.assertIsNone(latch.observe(False,1700))

    def test_refreshes_continuous_presence_at_exact_five_seconds(self):
        latch=ZoneLatch(1,2)
        self.assertEqual(latch.observe(True,1000),'person_in_zone')
        self.assertIsNone(latch.observe(True,5999))
        self.assertEqual(latch.observe(True,6000),'person_in_zone')
        self.assertIsNone(latch.observe(True,6001))

    def test_reset_needs_new_classification_after_session_change(self):
        latch=ZoneLatch(2,2)
        latch.observe(True,1); latch.observe(True,2); latch.reset()
        self.assertFalse(latch.present)
        self.assertIsNone(latch.observe(True,3))
        self.assertEqual(latch.observe(True,4),'person_in_zone')


class StateTests(unittest.TestCase):
    def test_persisted_queue_preserves_identity_id_and_sequence_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            state=SequenceStore(path,'desk','jetson')
            first=state.enqueue('session','person_in_zone',1000,{'detail':'test metadata'})
            restored=SequenceStore(path,'desk','jetson')
            self.assertEqual(restored.seq,1)
            self.assertEqual(restored.pending[0]['event_id'],first['event_id'])
            second=restored.enqueue('session','person_cleared',2000,{})
            self.assertEqual(second['seq'],2)
            self.assertFalse(second['simulated'])
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            with self.assertRaises(ValueError): SequenceStore(path,'other-desk','jetson')

    def test_prunes_expired_and_cross_session_evidence_without_resetting_seq(self):
        with tempfile.TemporaryDirectory() as directory:
            state=SequenceStore(Path(directory)/'state.json','desk','jetson')
            state.enqueue('old','person_in_zone',1000,{})
            state.enqueue('current','person_in_zone',1000,{})
            self.assertEqual(state.prune(121000,'current'),1)
            self.assertEqual(len(state.pending),1,'Exact120s boundary remains accepted')
            self.assertEqual(state.prune(121001,'current'),1)
            self.assertEqual(state.seq,2)

    def test_queue_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            state=SequenceStore(Path(directory)/'state.json','desk','jetson')
            state.pending=[{}]*128
            with self.assertRaisesRegex(RuntimeError,'queue full'):state.enqueue('session','heartbeat',1000,{})
            self.assertEqual(state.seq,0)

    def test_exclusive_process_lock_prevents_sequence_races(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            first=acquire_process_lock(path)
            try:
                with self.assertRaisesRegex(RuntimeError,'Another camera publisher'):acquire_process_lock(path)
            finally:first.close()
            second=acquire_process_lock(path);second.close()

    def test_credential_file_permissions_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'token.json'
            path.write_text(json.dumps({'token':'fake-token-for-unit-tests'}));path.chmod(0o644)
            with self.assertRaisesRegex(ValueError,'owner-only'):read_secret(path)
            path.chmod(0o600)
            self.assertEqual(read_secret(path),'fake-token-for-unit-tests')


class TransportTests(unittest.TestCase):
    def test_decodes_actual_spacetimedb_schema_columns(self):
        result=[{'schema':{'elements':[{'name':{'some':'id'}},{'name':{'some':'active_session_id'}}]},'rows':[['desk','session']]}]
        self.assertEqual(sql_rows(result),[{'id':'desk','active_session_id':'session'}])

    def test_rejects_credentials_and_nonorigin_database_uris(self):
        for uri in ['file:///tmp/db','https://user:password@example.com','https://example.com/path','https://example.com?token=x']:
            with self.subTest(uri=uri),self.assertRaises(ValueError):DatabaseClient(uri,'db','fake-token','desk','jetson')
        self.assertTrue(DatabaseClient('wss://example.com','db','fake-token','desk','jetson').base.startswith('https://'))

    def test_confirmed_reducer_arguments_keep_observation_provenance(self):
        client=DatabaseClient('http://127.0.0.1:3000','db','fake-token','desk','jetson')
        event={'workstation_id':'desk','session_id':'s','device_id':'jetson','event_id':'e','kind':'person_in_zone','observed_at':1000,'seq':2,'confidence':.7,'metadata':{'captureMode':'opencv_hog'},'simulated':False}
        with patch.object(client,'call') as call:
            client.observation(event)
            args=call.call_args.args
            self.assertEqual(args[0],'ingest_device_event')
            self.assertEqual(args[1][-2:],[False,1])
            self.assertEqual(args[1][:4],['desk','s','jetson','e'])


class MetricsTests(unittest.TestCase):
    def test_motion_only_is_always_degraded_for_person_coverage(self):
        metrics=Metrics('motion',False);metrics.frame(3)
        snapshot=metrics.snapshot()
        self.assertFalse(snapshot['person_classification_available'])
        self.assertEqual(snapshot['camera_status'],'degraded')
        self.assertFalse(snapshot['healthy'])

    def test_old_frame_is_offline_even_if_capture_thread_is_stuck(self):
        metrics=Metrics('hog',False);metrics.last_frame=10;metrics.started=0;metrics.update(camera_status='online')
        with patch('edge.jetson.airlock_camera.time.monotonic',return_value=30):
            snapshot=metrics.snapshot()
        self.assertEqual(snapshot['camera_status'],'offline')
        self.assertFalse(snapshot['healthy'])
        self.assertEqual(snapshot['frame_age_ms'],20000)

    def test_metrics_are_numeric_and_exclude_raw_details(self):
        metrics=Metrics('hog',False);metrics.update(last_error='private-token-must-not-appear')
        output=metrics_text(metrics.snapshot())
        self.assertNotIn('private-token',output)
        self.assertIn('airlock_camera_healthy 0',output)

    def test_lan_bind_requires_authentication(self):
        self.assertFalse(bind_requires_auth('127.0.0.1'))
        self.assertFalse(bind_requires_auth('::1'))
        self.assertTrue(bind_requires_auth('0.0.0.0'))
        with self.assertRaises(ValueError):serve_metrics(Metrics('hog',False),'0.0.0.0',0)

    def test_read_only_http_has_health_metrics_and_bearer_auth(self):
        server=serve_metrics(Metrics('hog',False),'127.0.0.1',0,'fake-api-token-for-tests')
        base=f'http://127.0.0.1:{server.server_address[1]}'
        try:
            with self.assertRaises(error.HTTPError) as denied:request.urlopen(base+'/metrics')
            self.assertEqual(denied.exception.code,401);denied.exception.close()
            req=request.Request(base+'/metrics',headers={'Authorization':'Bearer fake-api-token-for-tests'})
            with request.urlopen(req) as result:
                self.assertEqual(result.status,200);self.assertEqual(result.headers['Cache-Control'],'no-store')
                self.assertIn('airlock_camera_frames',result.read().decode())
            req=request.Request(base+'/health',headers={'Authorization':'Bearer fake-api-token-for-tests'})
            with self.assertRaises(error.HTTPError) as degraded:request.urlopen(req)
            self.assertEqual(degraded.exception.code,503);degraded.exception.close()
        finally:server.shutdown();server.server_close()


class DetectorTests(unittest.TestCase):
    def test_hog_uses_builtin_classifier_and_roi_without_loading_a_model_file(self):
        class Model:
            def setSVMDetector(self,weights):self.weights=weights
            def detectMultiScale(self,*_args,**_kwargs):return [(30,30,40,140),(500,300,20,30)],[.8,.9]
        model=Model()
        cv=SimpleNamespace(HOGDescriptor=lambda:model,HOGDescriptor_getDefaultPeopleDetector=lambda:['builtin'])
        detector=OpenCVDetector(cv,'hog',(0,0,.5,1),.03)
        detected,score,motion=detector.detect(SimpleNamespace(shape=(480,640,3)))
        self.assertTrue(detected);self.assertEqual(score,.8);self.assertEqual(motion,0)
        self.assertEqual(model.weights,['builtin'])

    def test_hog_rejects_too_small_frames(self):
        model=SimpleNamespace(setSVMDetector=lambda _:None)
        cv=SimpleNamespace(HOGDescriptor=lambda:model,HOGDescriptor_getDefaultPeopleDetector=lambda:[])
        detector=OpenCVDetector(cv,'hog',(0,0,1,1),.03)
        with self.assertRaisesRegex(ValueError,'64x128'):detector.detect(SimpleNamespace(shape=(100,40,3)))



class CaptureOptInTests(unittest.TestCase):
    def test_cli_keeps_camera_closed_without_explicit_capture_flag(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)/'state.json'
            result=subprocess.run([sys.executable,'edge/jetson/airlock_camera.py','--workstation','desk','--device','jetson','--state-file',str(state)],capture_output=True,text=True,check=False)
            self.assertEqual(result.returncode,2)
            self.assertIn('Camera remains closed',result.stderr)
            self.assertFalse(state.exists())

if __name__=='__main__':unittest.main()
