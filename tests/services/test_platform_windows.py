import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from homebrew_harness.platforms.http import JsonTransport
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.runtime.limits import limits_scope
import httpx


class PlatformWindows(unittest.TestCase):
    def setUp(self):
        self.calls=[];self.delay_first=True;outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def handle_call(self):
                outer.calls.append(self.command)
                if len(outer.calls)==1 and outer.delay_first:time.sleep(.25)
                data=json.dumps({'calls':len(outer.calls)}).encode()
                try:
                    self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError):pass
            do_GET=handle_call;do_POST=handle_call
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.client=JsonTransport('http://127.0.0.1:'+str(self.server.server_port),'synthetic');self.addCleanup(self.client.close)

    def test_read_timeout_has_bounded_retry_and_uses_configured_window(self):
        with limits_scope({'platform_read_seconds':.08,'platform_read_attempts':2}):
            result,_=self.client.request('GET','example',Deadline.after(3))
        self.assertEqual(result['calls'],2);self.assertEqual(self.calls,['GET','GET'])

    def test_uncertain_write_is_not_repeated_and_reports_the_actual_timeout(self):
        with limits_scope({'platform_read_seconds':.08,'platform_read_attempts':5}):
            with self.assertRaises(httpx.ReadTimeout) as error:self.client.request('POST','example',Deadline.after(3),body={'request':'one'})
        self.assertEqual(self.calls,['POST']);self.assertEqual(error.exception.details['limit_name'],'platform_read_seconds')
        self.assertEqual(error.exception.details['limit_value'],.08)

    def test_active_request_observes_cancellation_before_its_io_deadline(self):
        from homebrew_harness.execution.execution_control import control_scope
        stop=threading.Event();timer=threading.Timer(.08,stop.set);timer.start();self.addCleanup(timer.cancel)
        started=time.monotonic()
        with control_scope(Deadline.after(3), cancel_events=(stop,)):
            from homebrew_harness.execution.model_gateway import ModelCancelled
            with self.assertRaises(ModelCancelled):self.client.request('GET','example',Deadline.after(3))
        self.assertLess(time.monotonic()-started,1)
