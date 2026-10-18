"""End-to-end test for the WhatsApp AI lead qualification bot against MOCK APIs.

No real WhatsApp number, Claude account, GoHighLevel account or Slack workspace is used. The script:
  1. starts mock WhatsApp Cloud, Anthropic, GoHighLevel and Slack APIs on this machine,
  2. imports a temporary copy of the workflow into your n8n (API hosts pointed at the mocks, fake keys),
  3. plays 14 scenarios: Meta verification, forged webhooks, receipts, a photo, a full six-message chat,
     duplicates, a lead who answers everything at once, and outages of Claude, GoHighLevel and WhatsApp,
  4. checks every request the workflow made, prints PASS or FAIL per check, then deletes the temporary copy.

The mock Claude does not think: it returns scripted tool calls, but it rejects malformed requests the way the
real Messages API would (roles must alternate, the tool must be forced, and so on).

Requirements: Python 3.9+, a running n8n with the public API enabled.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
Usage: python tests/e2e_mock_test.py
"""
import copy, hashlib, hmac, json, os, re, sys, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8765
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
WORKFLOW = Path(__file__).resolve().parent.parent / 'workflow' / 'whatsapp-lead-qualification-bot.json'
PATH = 'whatsapp-lead-bot-e2e-test'
SECRET = 'test-app-secret'
ENV = {
    'WHATSAPP_APP_SECRET': SECRET, 'WHATSAPP_VERIFY_TOKEN': 'test-verify-token', 'WHATSAPP_TOKEN': 'mock-wa-token',
    'WHATSAPP_PHONE_NUMBER_ID': 'mock-phone-id', 'WHATSAPP_API_VERSION': 'v23.0',
    'ANTHROPIC_API_KEY': 'mock-anthropic-key', 'ANTHROPIC_MODEL': 'claude-sonnet-5',
    'BUSINESS_NAME': 'Acme Growth', 'BUSINESS_ABOUT': 'a marketing and automation agency',
    'GHL_API_KEY': 'mock-ghl-key', 'GHL_LOCATION_ID': 'mock-location', 'GHL_APP_URL': 'https://app.gohighlevel.com',
    'SLACK_WEBHOOK_URL': MOCK + '/slack/hook',
}
HOSTS = {'https://api.anthropic.com': MOCK, 'https://graph.facebook.com': MOCK, 'https://services.leadconnectorhq.com': MOCK}

A, B, C, D, E, F, G = ('15553334444', '15557778888', '15559990000', '15551230000', '15554445555', '15556667777', '15550002222')

# What the mock Claude answers, keyed by the newest user message. Fields left out come back empty.
SCRIPT = {
    'Hi there': dict(reply="Hi! Thanks for messaging Acme Growth. What's your name?"),
    "I'm Jamie Rivera": dict(reply='Nice to meet you, Jamie! What company or business are you with?', name='Jamie Rivera'),
    # On purpose, this answer leaves the name empty: the workflow must keep the name it already has.
    'Rivera Roofing in Austin': dict(reply="Thanks Jamie. What's the main challenge you want to fix right now?", company='Rivera Roofing'),
    'We miss calls and leads go cold': dict(reply='Got it. What monthly budget do you have in mind: under $500, $500 to $2,000, or above $2,000?',
                                            name='Jamie Rivera', company='Rivera Roofing', need='Missed calls, leads going cold'),
    'Around $1,500 a month': dict(reply='Great. When would you like to start: ASAP, 1 to 3 months, 3 to 6 months, or just exploring?',
                                  name='Jamie Rivera', company='Rivera Roofing', need='Missed calls, leads going cold',
                                  budget='Around $1,500 a month', budget_tier='500_to_2000'),
    'ASAP please': dict(reply='Thanks Jamie! I have Rivera Roofing, missed calls, around $1,500 a month, starting ASAP. A team member will reach out soon.',
                        intent='complete', name='Jamie Rivera', company='Rivera Roofing', need='Missed calls, leads going cold',
                        budget='Around $1,500 a month', budget_tier='500_to_2000', timeline='ASAP', timeline_tier='asap',
                        crm_note='Jamie owns Rivera Roofing in Austin and is losing jobs to missed calls. Budget around $1,500 a month, wants to start ASAP.'),
    'Do you work weekends?': dict(reply='Good question, Jamie! A team member will be in touch soon and can answer that.', intent='after_complete'),
    'Hello': dict(reply="Hi! Thanks for reaching out to Acme Growth. What's your name?"),
    "I'm Sam Cole from Cole Dental. We need online booking. Budget is about $3,000 a month and we want to start ASAP":
        dict(reply='Thanks Sam! I have Cole Dental, online booking, about $3,000 a month, starting ASAP. A team member will reach out soon.',
             intent='complete', name='Sam Cole', company='Cole Dental', need='Online booking', budget='About $3,000 a month',
             budget_tier='over_2000', timeline='ASAP', timeline_tier='asap', crm_note='Sam runs Cole Dental and wants online booking. Strong budget, ready now.'),
    'Hey\nIs this Acme Growth?': dict(reply="Yes, this is Acme Growth! What's your name?"),
    'Hi, quick question': 529,
    'Hello again': dict(reply="Hi! Happy to help. What's your name?"),
    "I'm Lee Park from Park Plumbing. We need more Google reviews, budget under $500 a month, timeline 1 to 3 months":
        dict(reply='Thanks Lee! I have Park Plumbing, more reviews, under $500 a month, in 1 to 3 months. A team member will reach out soon.',
             intent='complete', name='Lee Park', company='Park Plumbing', need='More Google reviews', budget='Under $500 a month',
             budget_tier='under_500', timeline='1 to 3 months', timeline_tier='1_to_3_months', crm_note='Lee wants more Google reviews for Park Plumbing.'),
    'Thanks!': dict(reply="You're welcome, Lee! A team member will be in touch soon.", intent='after_complete'),
    'Good morning': dict(reply="Good morning! What's your name?"),
}

LOG = []                                   # (service, path, headers, body)
FAIL = {'graph': set(), 'ghl': False}
COUNT = {'n': 0}


def claude_problems(headers, body):
    """The checks the real Messages API would fail a request on."""
    p = []
    if headers.get('x-api-key') != ENV['ANTHROPIC_API_KEY']:
        p.append('missing x-api-key')
    if headers.get('anthropic-version') != '2023-06-01':
        p.append('missing anthropic-version')
    if not isinstance(body.get('model'), str) or not isinstance(body.get('max_tokens'), int):
        p.append('model and max_tokens are required')
    if not isinstance(body.get('system'), str):
        p.append('system must be a string')
    msgs = body.get('messages') or []
    if not msgs or msgs[0].get('role') != 'user' or msgs[-1].get('role') != 'user':
        p.append('messages must start and end with the user')
    if any(a.get('role') == b.get('role') for a, b in zip(msgs, msgs[1:])):
        p.append('roles must alternate')
    if any(not isinstance(m.get('content'), str) or not m['content'] for m in msgs):
        p.append('empty message content')
    tools = body.get('tools') or [{}]
    if tools[0].get('input_schema', {}).get('type') != 'object' or body.get('tool_choice') != {'type': 'tool', 'name': tools[0].get('name')}:
        p.append('tool must be defined and forced')
    return p


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain' if isinstance(obj, str) else 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}'
        body = json.loads(raw)
        headers = {k.lower(): v for k, v in self.headers.items()}
        path = self.path.rstrip('/')
        service = ('claude' if path == '/v1/messages' else 'graph' if re.match(r'^/v\d+\.\d+/', path)
                   else 'ghl' if path.startswith('/contacts') else 'slack' if path.startswith('/slack') else 'other')
        LOG.append((service, path, headers, body))
        COUNT['n'] += 1

        if service == 'claude':
            problems = claude_problems(headers, body)
            if problems:
                return self._send(400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': '; '.join(problems)}})
            answer = SCRIPT.get(body['messages'][-1]['content'])
            if answer == 529:
                return self._send(529, {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}})
            if answer is None:
                return self._send(200, {'content': [{'type': 'text', 'text': 'no script for this message'}]})
            tool_input = {'intent': 'qualifying', 'name': '', 'company': '', 'need': '', 'budget': '', 'budget_tier': 'unknown',
                          'timeline': '', 'timeline_tier': 'unknown', 'crm_note': '', **answer}
            return self._send(200, {'id': 'msg_mock', 'type': 'message', 'role': 'assistant', 'model': body['model'],
                                    'content': [{'type': 'tool_use', 'id': 'toolu_mock', 'name': 'reply_to_lead', 'input': tool_input}],
                                    'stop_reason': 'tool_use', 'usage': {'input_tokens': 600, 'output_tokens': 120}})
        if service == 'graph':
            if headers.get('authorization') != 'Bearer ' + ENV['WHATSAPP_TOKEN'] or body.get('to') in FAIL['graph']:
                return self._send(401, {'error': {'message': 'Invalid OAuth access token.', 'type': 'OAuthException', 'code': 190}})
            return self._send(200, {'messaging_product': 'whatsapp', 'contacts': [{'input': body['to'], 'wa_id': body['to']}],
                                    'messages': [{'id': f"wamid.reply{COUNT['n']}"}]})
        if service == 'ghl':
            if FAIL['ghl'] or headers.get('authorization') != 'Bearer ' + ENV['GHL_API_KEY'] or headers.get('version') != '2021-07-28':
                return self._send(401, {'statusCode': 401, 'message': 'Invalid JWT'})
            if path == '/contacts/upsert':
                return self._send(200, {'new': True, 'contact': {'id': 'contact-' + body.get('phone', '').lstrip('+')}, 'traceId': 'mock'})
            if path.endswith('/tags'):
                return self._send(201, {'tags': body.get('tags', [])})
            if path.endswith('/notes'):
                return self._send(201, {'note': {'id': 'note-1', 'body': body.get('body')}})
        if service == 'slack':
            return self._send(200, 'ok')
        return self._send(404, {'message': 'not mocked'})


def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def test_copy():
    """The workflow with $env values replaced by test values and the API hosts pointed at the mocks."""
    wf = json.loads(WORKFLOW.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: "'" + ENV.get(m.group(1), '') + "'", value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        if node['type'] == 'n8n-nodes-base.webhook':
            node['parameters']['path'] = PATH
    return wf


class Runner:
    def __init__(self, wid):
        self.wid, self.sent, self.results, self.mark = wid, 0, [], 0

    def finished(self):
        return api('GET', f'/api/v1/executions?workflowId={self.wid}&limit=250').get('data', [])

    def wait(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            done = [e for e in self.finished() if e.get('stoppedAt')]
            if len(done) >= self.sent:
                return max(done, key=lambda e: int(e['id']))
            time.sleep(0.4)
        raise TimeoutError('the workflow did not finish in time')

    def post(self, payload, signed=True, signature=None):
        self.mark = len(LOG)
        data = json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json'}
        if signed:
            headers['X-Hub-Signature-256'] = 'sha256=' + hmac.new(SECRET.encode(), data, hashlib.sha256).hexdigest()
        if signature:
            headers['X-Hub-Signature-256'] = signature
        urllib.request.urlopen(urllib.request.Request(f'{N8N}/webhook/{PATH}', method='POST', data=data, headers=headers), timeout=30)
        self.sent += 1
        return self.wait()

    def get(self, query):
        self.sent += 1
        try:
            with urllib.request.urlopen(f'{N8N}/webhook/{PATH}?{urlencode(query)}', timeout=30) as r:
                result = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            result = e.code, e.read().decode()
        self.wait()
        return result

    def stopped_at(self, ex):
        """The node a run ended on, and its error message if it failed."""
        data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}
        result = data.get('resultData') or {}
        return result.get('lastNodeExecuted'), (result.get('error') or {}).get('message', '')

    def new(self, service, to=None):
        """Requests to one mock service since the last webhook was sent."""
        return [e for e in LOG[self.mark:] if e[0] == service and (to is None or e[3].get('to') == to)]

    def check(self, scenario, label, ok, detail=''):
        self.results.append((scenario, label, bool(ok), detail))


def msg(phone, text, mid):
    return {'from': phone, 'id': mid, 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}


def webhook(messages=None, names=(), statuses=None):
    value = {'messaging_product': 'whatsapp', 'metadata': {'display_phone_number': '15550001111', 'phone_number_id': 'mock-phone-id'}}
    if names:
        value['contacts'] = [{'profile': {'name': n}, 'wa_id': p} for p, n in names]
    if messages:
        value['messages'] = messages
    if statuses:
        value['statuses'] = statuses
    return {'object': 'whatsapp_business_account', 'entry': [{'id': 'mock-waba', 'changes': [{'value': value, 'field': 'messages'}]}]}


def text_of(entry):
    return entry[3]['text']['body']


def run(r):
    # 1. Meta webhook verification (GET on the same URL that receives messages)
    status, body = r.get({'hub.mode': 'subscribe', 'hub.verify_token': 'test-verify-token', 'hub.challenge': '1158201444'})
    r.check('Meta verification, right token', 'returns the challenge with 200', status == 200 and body == '1158201444', f'{status} {body!r}')
    status, body = r.get({'hub.mode': 'subscribe', 'hub.verify_token': 'wrong', 'hub.challenge': '1158201444'})
    r.check('Meta verification, wrong token', 'refused with 403', status == 403, f'{status} {body!r}')

    # 2. Forged webhooks
    for label, kwargs in (('wrong signature', {'signature': 'sha256=' + '0' * 64}), ('no signature', {})):
        ex = r.post(webhook([msg(A, 'Hi there', 'wamid.forged-' + label[:2])], [(A, 'Jamie R')]), signed=False, **kwargs)
        node, _ = r.stopped_at(ex)
        r.check(f'Forged webhook, {label}', 'stopped at the signature check, nothing sent',
                not r.new('claude') and not r.new('graph') and node == 'Stop: Bad Signature', f'ended at {node}')

    # 3. Delivery receipts only
    ex = r.post(webhook(statuses=[{'id': 'wamid.reply1', 'status': 'delivered', 'recipient_id': A, 'timestamp': '1'}]))
    r.check('Delivery receipt', 'ignored, nothing sent', not r.new('claude') and not r.new('graph') and ex['status'] == 'success', ex['status'])

    # 4. A photo instead of text
    r.post(webhook([{'from': D, 'id': 'wamid.D1', 'timestamp': '1', 'type': 'image', 'image': {'id': 'media-1', 'mime_type': 'image/jpeg'}}], [(D, 'Dana')]))
    sent = r.new('graph', D)
    r.check('Photo instead of text', 'polite ask for text, Claude not called', len(sent) == 1 and 'only read text' in text_of(sent[0]) and not r.new('claude'),
            text_of(sent[0]) if sent else 'no reply')

    # 5. Full six message chat
    turns = ['Hi there', "I'm Jamie Rivera", 'Rivera Roofing in Austin', 'We miss calls and leads go cold', 'Around $1,500 a month', 'ASAP please']
    sizes, replies, systems = [], [], []
    for i, text in enumerate(turns, 1):
        r.post(webhook([msg(A, text, f'wamid.A{i}')], [(A, 'Jamie R')]))
        call = r.new('claude')
        sizes.append(len(call[0][3]['messages']) if call else 0)
        systems.append(call[0][3]['system'] if call else '')
        replies += [text_of(e) for e in r.new('graph', A)]
        if i == 6:
            ghl, slack = r.new('ghl'), r.new('slack')
    r.check('Full chat (6 messages)', 'one reply per message, in order', replies == [SCRIPT[t]['reply'] for t in turns], f'{len(replies)} replies')
    r.check('Full chat (6 messages)', 'Claude gets the growing chat history', sizes == [1, 3, 5, 7, 9, 11], f'messages sent to Claude per turn: {sizes}')
    r.check('Full chat (6 messages)', 'a detail is kept when a later answer leaves it empty',
            'Name: Jamie Rivera' in systems[3] and 'Company: Rivera Roofing' in systems[3], 'turn 4 prompt knows name and company')
    upserts = [e for e in ghl if e[1] == '/contacts/upsert']
    up = upserts[0][3] if upserts else {}
    r.check('Full chat (6 messages)', 'one GoHighLevel upsert with the right fields',
            len(upserts) == 1 and up.get('phone') == '+' + A and up.get('firstName') == 'Jamie' and up.get('lastName') == 'Rivera'
            and up.get('companyName') == 'Rivera Roofing' and up.get('locationId') == 'mock-location', json.dumps(up))
    tags = [e[3]['tags'] for e in ghl if e[1].endswith('/tags')]
    r.check('Full chat (6 messages)', 'tags added with the score (9/10, hot lead)', tags == [['whatsapp-lead', 'ai-qualified', 'score-9', 'hot-lead']], json.dumps(tags))
    notes = [e[3]['body'] for e in ghl if e[1].endswith('/notes')]
    r.check('Full chat (6 messages)', 'note with every detail and the AI summary',
            len(notes) == 1 and 'Budget: Around $1,500 a month' in notes[0] and 'losing jobs to missed calls' in notes[0], notes[0] if notes else '')
    alert = slack[0][3]['text'] if slack else ''
    r.check('Full chat (6 messages)', 'Slack alert with score and contact link',
            'New WhatsApp lead' in alert and 'score 9/10' in alert and '/contacts/detail/contact-' + A in alert, alert.splitlines()[0] if alert else 'no alert')

    # 6. WhatsApp delivers the same message twice
    r.post(webhook([msg(A, 'ASAP please', 'wamid.A6')], [(A, 'Jamie R')]))
    r.check('Same message delivered twice', 'second copy ignored', not r.new('claude') and not r.new('graph'), 'no Claude call, no reply')

    # 7. The qualified lead writes again
    r.post(webhook([msg(A, 'Do you work weekends?', 'wamid.A7')], [(A, 'Jamie R')]))
    call = r.new('claude')
    r.check('Qualified lead writes again', 'friendly reply, no second lead in GoHighLevel',
            call and 'already qualified' in call[0][3]['system'] and len(r.new('graph', A)) == 1 and not r.new('ghl') and not r.new('slack'),
            'reply sent, 0 GoHighLevel calls')

    # 8. Everything in one message
    r.post(webhook([msg(B, 'Hello', 'wamid.B1')], [(B, 'Sam')]))
    r.post(webhook([msg(B, "I'm Sam Cole from Cole Dental. We need online booking. Budget is about $3,000 a month and we want to start ASAP", 'wamid.B2')], [(B, 'Sam')]))
    tags = [e[3]['tags'] for e in r.new('ghl') if e[1].endswith('/tags')]
    r.check('Answers everything at once', 'qualified in one step, score 10/10',
            len([e for e in r.new('ghl') if e[1] == '/contacts/upsert']) == 1 and tags == [['whatsapp-lead', 'ai-qualified', 'score-10', 'hot-lead']], json.dumps(tags))

    # 9. Two messages in one webhook
    r.post(webhook([msg(C, 'Hey', 'wamid.C1'), msg(C, 'Is this Acme Growth?', 'wamid.C2')], [(C, 'Chris')]))
    call = r.new('claude')
    r.check('Two messages in one webhook', 'joined into one turn, one reply',
            len(call) == 1 and call[0][3]['messages'][-1]['content'] == 'Hey\nIs this Acme Growth?' and len(r.new('graph', C)) == 1, f'{len(call)} Claude call(s)')

    # 10. Claude is down
    ex = r.post(webhook([msg(E, 'Hi, quick question', 'wamid.E1')], [(E, 'Erin')]))
    sent, (node, _) = r.new('graph', E), r.stopped_at(ex)
    r.check('Claude is down (529)', 'retried 3 times, holding reply sent, run flagged for the alert',
            len(r.new('claude')) == 3 and len(sent) == 1 and 'trouble on my side' in text_of(sent[0]) and node == 'Stop: AI Failed',
            f"{len(r.new('claude'))} attempts, ended at {node}")
    r.post(webhook([msg(E, 'Hello again', 'wamid.E2')], [(E, 'Erin')]))
    call = r.new('claude')
    r.check('Claude is back', 'the chat continues normally', call and len(call[0][3]['messages']) == 1 and len(r.new('graph', E)) == 1, 'reply sent')

    # 11. GoHighLevel is down when the lead completes, then recovers
    FAIL['ghl'] = True
    r.post(webhook([msg(F, "I'm Lee Park from Park Plumbing. We need more Google reviews, budget under $500 a month, timeline 1 to 3 months", 'wamid.F1')], [(F, 'Lee')]))
    alert = r.new('slack')[0][3]['text'] if r.new('slack') else ''
    r.check('GoHighLevel is down', 'retried, Slack says NOT saved, lead still got a reply',
            len([e for e in r.new('ghl') if e[1] == '/contacts/upsert']) == 3 and 'NOT saved' in alert and len(r.new('graph', F)) == 1, alert.splitlines()[0] if alert else 'no alert')
    FAIL['ghl'] = False
    r.post(webhook([msg(F, 'Thanks!', 'wamid.F2')], [(F, 'Lee')]))
    alert = r.new('slack')[0][3]['text'] if r.new('slack') else ''
    r.check('GoHighLevel is back', 'the save is retried on the next message', len([e for e in r.new('ghl') if e[1] == '/contacts/upsert']) == 1
            and 'New WhatsApp lead' in alert and 'score 6/10' in alert, alert.splitlines()[0] if alert else 'no alert')

    # 12. WhatsApp rejects the reply
    FAIL['graph'].add(G)
    ex = r.post(webhook([msg(G, 'Good morning', 'wamid.G1')], [(G, 'Gale')]))
    node, error = r.stopped_at(ex)
    r.check('WhatsApp rejects the reply', 'retried 3 times, run flagged for the alert with the phone number',
            len(r.new('graph', G)) == 3 and node == 'Stop: Reply Failed' and '+' + G in error, f'ended at {node}')


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    server = ThreadingHTTPServer(('127.0.0.1', MOCK_PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    wf = test_copy()
    wid = api('POST', '/api/v1/workflows', {'name': 'TEST WhatsApp lead bot e2e (temporary)', 'nodes': wf['nodes'],
                                            'connections': wf['connections'], 'settings': wf.get('settings', {})})['id']
    r = Runner(wid)
    try:
        api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(2)
        run(r)
    finally:
        try:
            api('POST', f'/api/v1/workflows/{wid}/deactivate')
        finally:
            api('DELETE', f'/api/v1/workflows/{wid}')
            server.shutdown()

    passed = sum(ok for *_, ok, _ in r.results)
    last = None
    for scenario, label, ok, detail in r.results:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f'  ({detail})' if detail else ''))
    print(f'\n{passed}/{len(r.results)} checks passed')
    sys.exit(0 if passed == len(r.results) else 1)


if __name__ == '__main__':
    main()
