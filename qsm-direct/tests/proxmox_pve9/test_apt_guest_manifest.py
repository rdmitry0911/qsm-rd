#!/usr/bin/python3
"""Keep historical releases valid while admitting a complete guest release."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('qsm_apt_publish',ROOT/'packaging/apt/publish.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class GuestManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((ROOT/'packaging/apt/releases.json').read_text())

    def extended(self):
        value = copy.deepcopy(self.manifest)
        release = copy.deepcopy(value['releases'][-1])
        release['tag'] = 'v0.4.0-test-guest'
        version = '0.4.0+test-guest'
        assets = []
        for name, architectures in publisher.PACKAGES.items():
            arch = 'amd64' if name == 'qsm-pve-direct-lxc' else architectures[0]
            assets.append({'package':name,'version':version,'architecture':arch,
                'filename':f'{name}_{version}_{arch}.deb','sha256':'a'*64})
        release['assets'] = assets
        value['releases'].append(release)
        return value

    def test_historical_and_complete_extended_releases(self):
        publisher.validate_manifest(self.manifest)
        publisher.validate_manifest(self.extended())

    def test_tilde_version_downloads_under_the_name_github_stores(self):
        version = '0.4.0+test~tilde'
        value = self.extended()
        for asset in value['releases'][-1]['assets']:
            asset['version'] = version
            asset['filename'] = f"{asset['package']}_{version}_{asset['architecture']}.deb"
        publisher.validate_manifest(value)
        self.assertEqual(publisher.github_asset_name(f'qsm-pve-direct_{version}_amd64.deb'),
            'qsm-pve-direct_0.4.0+test.tilde_amd64.deb')
        self.assertEqual(publisher.github_asset_name('qsm-pve-direct_0.4.0+stock4.13_amd64.deb'),
            'qsm-pve-direct_0.4.0+stock4.13_amd64.deb')

    def test_partial_guest_pair_and_unapproved_packages_are_rejected(self):
        for missing in ('qsm-desktop-agent','qsm-qt-input-geometry'):
            value = self.extended()
            value['releases'][-1]['assets'] = [a for a in value['releases'][-1]['assets'] if a['package']!=missing]
            with self.subTest(missing=missing),self.assertRaises(ValueError):publisher.validate_manifest(value)
        value = self.extended();value['releases'][-1]['assets'][0]['package'] = 'qemu-server'
        with self.assertRaises(ValueError):publisher.validate_manifest(value)

    def test_wrong_guest_architecture_and_mixed_versions_are_rejected(self):
        for field,bad in (('architecture','all'),('version','9.9.9')):
            value = self.extended()
            asset = next(a for a in value['releases'][-1]['assets'] if a['package']=='qsm-qt-input-geometry')
            asset[field] = bad
            with self.subTest(field=field),self.assertRaises(ValueError):publisher.validate_manifest(value)
        value = self.extended()
        asset = next(a for a in value['releases'][-1]['assets'] if a['package']=='qsm-pve-direct-lxc')
        asset.update(architecture='all',filename=f"qsm-pve-direct-lxc_{asset['version']}_all.deb")
        with self.assertRaises(ValueError):publisher.validate_manifest(value)


if __name__=='__main__':unittest.main()
