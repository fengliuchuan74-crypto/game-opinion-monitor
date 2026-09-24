import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

from collectors.app_store import AppStoreCollector, safe_artwork_url
from modules.app_icons import FRESH_SECONDS, _download_icon, game_hero_html, get_app_icon

ARTWORK='https://is1-ssl.mzstatic.com/image/thumb/icon/512x512bb.jpg'


class AppIconTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def collector(self,payload):
        session=Mock()
        session.get.return_value.status_code=200
        session.get.return_value.json.return_value=payload
        return AppStoreCollector(self.root,session=session),session

    def test_lookup_matches_exact_id_and_requests_selected_storefront(self):
        client,session=self.collector({'results':[
            {'trackId':222,'artworkUrl512':ARTWORK},
            {'trackId':111,'trackName':'目标游戏','artworkUrl512':ARTWORK}]})
        info,errors=client.lookup_app('111','HK')
        self.assertFalse(errors)
        self.assertEqual(info['name'],'目标游戏')
        self.assertEqual(info['artwork_url'],ARTWORK)
        self.assertEqual(session.get.call_args.kwargs['params']['country'],'hk')
        client,_=self.collector({'results':[{'trackId':222,'artworkUrl512':ARTWORK}]})
        self.assertIsNone(client.lookup_app('111','cn')[0])

    def test_search_keeps_official_artwork_and_falls_back_to_smaller_size(self):
        client,_=self.collector({'results':[{'trackId':111,'artworkUrl512':'https://other.test/a.png','artworkUrl100':ARTWORK}]})
        results,errors=client.search_apps('游戏','cn')
        self.assertFalse(errors)
        self.assertEqual(results[0]['artwork_url'],ARTWORK)

    def test_invalid_metadata_is_a_recoverable_missing_icon(self):
        for payload in [{'results':None},[],{'results':[None,{'trackId':None}]}]:
            client,_=self.collector(payload)
            self.assertIsNone(client.lookup_app('111')[0])
        self.assertEqual(safe_artwork_url('https://mzstatic.com.other.test/icon.png'),'')
        self.assertEqual(safe_artwork_url('https://user:password@is1-ssl.mzstatic.com/icon.png'),'')

    def test_offline_refresh_keeps_good_icon_and_throttles_failures(self):
        icon='data:image/png;base64,cached'
        with patch('modules.app_icons.time.time',return_value=100),patch('modules.app_icons._download_icon',return_value=icon):
            self.assertEqual(get_app_icon('111','cn',self.root,ARTWORK),icon)
        with patch('modules.app_icons.time.time',return_value=101),patch.object(AppStoreCollector,'lookup_app') as lookup:
            self.assertEqual(get_app_icon('111','cn',self.root),icon)
            lookup.assert_not_called()
        with patch('modules.app_icons.time.time',return_value=101+FRESH_SECONDS),patch.object(AppStoreCollector,'lookup_app',side_effect=requests.Timeout) as lookup:
            self.assertEqual(get_app_icon('111','cn',self.root),icon)
            self.assertEqual(get_app_icon('111','cn',self.root),icon)
            lookup.assert_called_once()

    def test_switching_game_or_country_does_not_reuse_another_icon(self):
        with patch('modules.app_icons._download_icon',side_effect=['data:image/png;base64,cn','data:image/png;base64,us']):
            get_app_icon('111','cn',self.root,ARTWORK)
            get_app_icon('111','us',self.root,ARTWORK)
        self.assertEqual(get_app_icon('111','cn',self.root,allow_fetch=False),'data:image/png;base64,cn')
        self.assertEqual(get_app_icon('111','us',self.root,allow_fetch=False),'data:image/png;base64,us')
        self.assertEqual(get_app_icon('222','cn',self.root,allow_fetch=False),'')

    def test_first_fetch_failure_is_cached_and_corrupt_cache_recovers(self):
        (self.root/'111_cn.json').write_text('{incomplete',encoding='utf-8')
        with patch.object(AppStoreCollector,'lookup_app',return_value=(None,['Unavailable'])) as lookup:
            self.assertEqual(get_app_icon('111','cn',self.root),'')
            self.assertEqual(get_app_icon('111','cn',self.root),'')
            lookup.assert_called_once()

    def test_download_resizes_real_image_and_refuses_redirects(self):
        source=io.BytesIO()
        Image.new('RGB',(512,512),'red').save(source,format='JPEG')
        session=Mock()
        response=session.get.return_value.__enter__=Mock()
        session.get.return_value.__exit__=Mock(return_value=False)
        response.return_value.status_code=200
        response.return_value.iter_content.return_value=[source.getvalue()]
        uri=_download_icon(session,ARTWORK)
        with Image.open(io.BytesIO(base64.b64decode(uri.split(',')[1]))) as result:
            self.assertEqual(result.size,(192,192))
            self.assertEqual(result.format,'PNG')
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        response.return_value.status_code=302
        self.assertEqual(_download_icon(session,ARTWORK),'')

    def test_hero_escapes_names_and_has_an_offline_placeholder(self):
        profile={'app_name':'<script>alert(1)</script>','country':'cn'}
        rendered=game_hero_html(profile)
        self.assertNotIn('<script>',rendered)
        self.assertIn('hero-icon-placeholder',rendered)
        self.assertIn('hero-game-icon',game_hero_html(profile,'data:image/png;base64,test'))


if __name__=='__main__': unittest.main()
