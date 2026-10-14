"""Exercise the bundled detector inside an isolated add-on smoke-test container."""
import base64
from io import BytesIO
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen

from PIL import Image

BASE = 'http://127.0.0.1:8000'
MODEL = {'id': 991, 'name': 'addon persistence fixture', 'threshold': 0.7,
         'classes': [{'id': 1, 'name': 'red'}, {'id': 2, 'name': 'blue'}]}


def image(color):
    out = BytesIO()
    Image.new('RGB', (640, 640), color).save(out, format='JPEG')
    return out.getvalue()


def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request(BASE + path, data=data, headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=90) as response:
        return json.load(response)


def upload(path, payload):
    boundary = 'addon-internal-detector-test'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="fixture.jpg"\r\n'
            'Content-Type: image/jpeg\r\n\r\n').encode() + payload + f'\r\n--{boundary}--\r\n'.encode()
    req = Request(BASE + path, data=body, headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    with urlopen(req, timeout=90) as response:
        return json.load(response)


if sys.argv[1] == 'initialize':
    assert request('/health')['status'] == 'ok'
    assert upload('/api/v1/detect', image('black'))['backend'] == 'onnx'
    faces = upload('/api/v1/faces/detect', image('black'))
    assert faces['success'], faces
    request('/api/v1/faces/list')
    # Seed a synthetic identity without using a real person's biometric data.
    sys.path.insert(0, '/opt/light-object-detect')
    import numpy as np
    from utils.face_db import FaceDB
    FaceDB('/data/detector/faces.db').add_face('addon persistence fixture', np.ones(512, dtype=np.float32))
    training = {'model': MODEL, 'samples': [
        {'class_id': i, 'image_base64': base64.b64encode(image(color)).decode()}
        for i, color in [(1, 'red'), (2, 'blue')]]}
    assert request('/api/v1/classification/train', training)['success']

elif sys.argv[1] == 'verify':
    result = request('/api/v1/classification/classify', {
        'model': MODEL, 'image_base64': base64.b64encode(image('red')).decode()})
    assert result['success'] and result['result'] == 'red', result
    assert 'addon persistence fixture' in json.dumps(request('/api/v1/faces/list'))
    assert Path('/data/detector/classification/model_991.json').exists()
else:
    raise ValueError('Expected initialize or verify')
print('INTERNAL_DETECTOR_PASS ' + sys.argv[1])
