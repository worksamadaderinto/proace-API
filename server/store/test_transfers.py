from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from core.models import User
from product.models import Product
from store.models import Store, DigitalUpload
from store.transfers import transfer
from rest_framework.exceptions import ValidationError
import tempfile

@override_settings(ROOT_URLCONF='storefront.urls',STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'}})
class TransferTests(TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        setting=override_settings(MEDIA_ROOT=self.directory.name);setting.enable();self.addCleanup(setting.disable)
        self.owner=User.objects.create_user(email='transfer-owner@example.test',password='fixture')
        self.other=User.objects.create_user(email='transfer-other@example.test',password='fixture')
        self.store=Store.objects.create(user=self.owner,name='Fixture',status='active',verified_at=timezone.now())
        self.product=Product.objects.create(store=self.store,title='Fixture video',is_digital=True,visibility=False,price=10,discount=0,available=0,image_url='https://example.test/cover.jpg')
        self.url=f'/api/v1/stores/{self.store.pk}/products/{self.product.pk}/digital-upload/'
        self.client=APIClient();self.client.force_authenticate(self.owner)
    def start(self,size=12):
        return self.client.post(self.url,{'operation':'start','filename':'video.mp4','size':size,'fingerprint':'a'*64},format='json')
    @patch('store.transfers.CHUNK_BYTES',8)
    def test_resume_part_integrity_finalization_and_isolation(self):
        response=self.start();self.assertEqual(response.status_code,200,response.data);identity=response.data['id']
        self.assertEqual(self.client.post(self.url,{'operation':'complete','upload':identity},format='json').status_code,400)
        for index,content in [(1,b'abcdefgh'),(2,b'ijkl')]:
            result=self.client.post(self.url,{'operation':'part','upload':identity,'part':index,'file':SimpleUploadedFile('part',content)},format='multipart')
            self.assertEqual(result.status_code,200,result.data)
        self.assertEqual(self.start().data['parts'],[1,2])
        changed=self.client.post(self.url,{'operation':'part','upload':identity,'part':1,'file':SimpleUploadedFile('part',b'12345678')},format='multipart')
        self.assertEqual(changed.status_code,400)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post(self.url,{'operation':'complete','upload':identity},format='json').status_code,404)
        self.client.force_authenticate(self.owner)
        result=self.client.post(self.url,{'operation':'complete','upload':identity},format='json')
        self.assertEqual(result.status_code,200,result.data)
        self.product.refresh_from_db()
        self.assertEqual((Path(self.directory.name)/self.product.digital_file_url).read_bytes(),b'abcdefghijkl')
        self.assertEqual(self.start().data['status'],'complete')
        self.assertEqual(self.client.post(self.url,{'operation':'complete','upload':identity},format='json').status_code,200)
    def test_four_gib_can_start_without_allocating_a_whole_file(self):
        response=self.start(4*1024**3)
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data['parts'],[])
        self.assertEqual(response.data['size'],4*1024**3)
    def test_expired_session_and_oversize_are_rejected(self):
        response=self.start();identity=response.data['id']
        DigitalUpload.objects.filter(pk=identity).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.client.post(self.url,{'operation':'sign','upload':identity,'part':1},format='json').status_code,400)
        self.assertEqual(self.start(6*1024**3).status_code,400)

    @patch('store.transfers.CHUNK_BYTES', 8)
    def test_local_completion_recovers_after_database_rollback(self):
        identity = self.start().data['id']
        for index, content in [(1, b'abcdefgh'), (2, b'ijkl')]:
            self.client.post(self.url, {'operation': 'part', 'upload': identity, 'part': index, 'file': SimpleUploadedFile('part', content)}, format='multipart')
        with patch.object(Product, 'save', side_effect=OSError('controlled database failure')):
            self.assertEqual(self.client.post(self.url, {'operation': 'complete', 'upload': identity}, format='json').status_code, 503)
        upload = DigitalUpload.objects.get(pk=identity)
        self.assertEqual(upload.status, 'uploading')
        for part in upload.parts.values():
            self.assertTrue((Path(self.directory.name) / part['name']).exists())
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, {'operation': 'complete', 'upload': identity}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.product.refresh_from_db()
        self.assertEqual((Path(self.directory.name) / self.product.digital_file_url).read_bytes(), b'abcdefghijkl')
