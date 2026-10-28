import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'extensions'))
from direct_guest.qsm_desktop_priority import DesktopPriority, HINT, intersection


class DesktopPriorityTests(unittest.TestCase):
    def setUp(self):
        self.value = {'version':1,'session':'test','sequence':1,'generation':1,'available':True,'locked':False,
            'capabilities':['windows','focus','caret'],'desktop':{'rect':[0,0,1280,800],'scale':1},
            'windows':[{'id':'input','pid':42,'rect':[100,100,800,500],'client_rect':[100,136,800,464],
                'visible':True,'opacity':1}], 'focus':'input','damage':[],
            'caret':{'window':'input','rect':[400,300,8,16]}}
        self.model = DesktopPriority()

    def read(self, value): return HINT.unpack(self.model.update(value))

    def test_same_layout_survives_heartbeats_and_caret_changes(self):
        first = self.read(self.value)
        self.assertEqual(first[0],3)
        self.value['sequence'] += 1; self.value['caret']['rect'][0] += 10
        second = self.read(self.value)
        self.assertEqual(first[1],second[1]); self.assertNotEqual(first[4],second[4])

    def test_geometry_changes_revoke_the_old_identity_even_with_a_forged_generation(self):
        first = self.read(self.value)
        self.value['windows'][0]['rect'][0] += 1
        self.value['windows'][0]['client_rect'][0] += 1
        self.assertNotEqual(self.read(self.value)[1], first[1])

    def test_window_above_the_caret_disables_early_text(self):
        upper = {'id':'cover','pid':43,'rect':[380,290,100,100],'client_rect':[380,290,100,100],'visible':True,'opacity':.5}
        self.value['windows'].append(upper)
        self.assertEqual(self.read(self.value)[0],1)
        upper['visible']=False
        self.assertEqual(self.read(self.value)[0],3)

    def test_partial_occlusion_clips_the_priority_area(self):
        upper = {'id':'cover','pid':43,'rect':[450,100,100,500],'client_rect':[450,100,100,500],'visible':True,'opacity':1}
        self.value['windows'].append(upper)
        hint = self.read(self.value)
        self.assertEqual(hint[0],3)
        self.assertIsNone(intersection(list(hint[4:]),upper['rect']))

    def test_same_application_video_cannot_enter_the_input_clip(self):
        self.value['version'] = 2
        self.value['caret']['clip'] = [390,290,30,40]
        hint = self.read(self.value)
        self.assertEqual(hint[0],3)
        self.assertEqual(list(hint[4:]),[390,290,30,40])
        self.assertIsNone(intersection(list(hint[4:]),[420,290,400,300]))
        # Moving the insertion point within one field keeps its scene, while
        # switching/resizing the input component retires old leading images.
        self.value['caret']['rect'][0] += 1
        self.assertEqual(self.read(self.value)[1],hint[1])
        self.value['caret']['clip'][2] += 1
        self.assertNotEqual(self.read(self.value)[1],hint[1])

    def test_transparency_unknown_client_and_missing_caret_retain_pixel_fallback(self):
        for mutation in ['opacity','client_rect','caret']:
            value=copy.deepcopy(self.value)
            if mutation=='opacity':value['windows'][0]['opacity']=.9
            elif mutation=='client_rect':value['windows'][0]['client_rect']=None
            else:value['caret']=None
            self.assertEqual(self.read(value)[0],1)
        self.assertEqual(self.read(None),(0,0,0,0,0,0,0,0))

    def test_negative_desktop_origin_is_removed_before_worker_mapping(self):
        self.value['desktop']['rect']=[-1280,-800,2560,1600]
        hint=self.read(self.value)
        self.assertEqual(hint[2:4],(2560,1600))
        self.assertGreaterEqual(hint[4],1280)


if __name__=='__main__':unittest.main(verbosity=2)
