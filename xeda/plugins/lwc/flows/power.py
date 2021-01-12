# © 2021 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import copy
import logging
import os
import math
import csv
from pathlib import Path
import re

from xeda.flows.flow import DesignSource, SimFlow
from xeda.flows.vivado.vivado_sim import VivadoPostsynthSim, VivadoSim
from xeda.flows.vivado.vivado_power import VivadoPower


_logger = logging.getLogger()


__all__ = ['VivadoPowerLwc', 'VivadoPowerTimingOnly']


_default_power_tvs = ['enc_16_0', 'enc_0_16', 'enc_1536_0',
                     'enc_0_1536', 'dec_16_0', 'dec_1536_0']

class VivadoPowerLwc(VivadoPower):
    required_settings = {}

    @classmethod
    def prerequisite_flows(cls, flow_settings, design_settings):

        parent_prereqs = VivadoPower.prerequisite_flows(
            flow_settings, design_settings)

        flow_overrides, design_overrides = parent_prereqs[VivadoPostsynthSim]

        flow_overrides['clock_period'] = flow_settings.get(
            'clock_period', 13.333)
        flow_overrides['optimize_power'] = flow_settings.get(
            'optimize_power', True)
        flow_overrides['prerun_time'] = 100 + \
            (flow_overrides['clock_period'] * 4) - 1
        flow_overrides['timing_sim'] = True
        flow_overrides['stop_time'] = None
        flow_overrides['vcd'] = None

        tb_settings = design_settings['tb']
        design_name = design_settings['name']
        lwc_settings = design_settings.get('lwc', {})

        power_tvs = flow_settings.get('power_tvs')
        if not power_tvs:
            power_tvs = _default_power_tvs
            algorithms = lwc_settings.get('algorithm')
            if (algorithms and (isinstance(algorithms, list) or isinstance(algorithms, tuple)) and len(algorithms) > 1) or lwc_settings.get('supports_hash'):
                power_tvs.extend(['hash_16', 'hash_1536'])

        lwc_variant = lwc_settings.get('variant')
        if not lwc_variant:
            name_splitted = design_name.split('-')
            assert len(
                name_splitted) > 1, "either specify design.lwc.variant or design.name should be ending with -v\d+"
            lwc_variant = name_splitted[-1]
            assert re.match(
                r'v\d+', lwc_variant), "either specify design.lwc.variant or design.name should be ending with -v\d+"
        power_tvs_root = os.path.join('KAT_GMU', lwc_variant)

        def pow_tv_run_config(tv_sub):
            tv_generics = copy.deepcopy(tb_settings.get('generics', {}))
            tv_generics['G_MAX_FAILURES'] = 1
            tv_generics['G_TEST_MODE'] = 0
            tv_generics['G_FNAME_LOG'] = f'{tv_sub}_LWCTB_log.txt'
            for t in ['pdi', 'sdi', 'do']:
                tv_generics[f'G_FNAME_{t.upper()}'] = DesignSource(
                    os.path.join(power_tvs_root, tv_sub, f'{t}.txt'))
            saif = f'{tv_sub}.saif'
            return dict(generics=tv_generics, saif=str(saif), report=f'{tv_sub}.xml', name=tv_sub)

        flow_overrides['run_configs'] = [
            pow_tv_run_config(tv) for tv in power_tvs]
        design_overrides['tb'] = design_overrides.get('tb', {})
        if 'configuration_specification' not in tb_settings:
            _logger.info(
                "setting design.tb.configuration_specification = LWC_TB_wrapper_conf")
            design_overrides['tb']["configuration_specification"] = "LWC_TB_wrapper_conf"

        return {VivadoPostsynthSim: (flow_overrides, design_overrides)}

    def parse_reports(self):
        design_name = self.settings.design['name']

        clock_period = self.synth_settings['clock_period']

        freq = math.floor(1000 / clock_period)
        fields = {'Design': design_name,
                  'Frequency (MHz)': freq, 'Static': None}

        timing_pat = re.compile(
            r"PASS \(0\): SIMULATION FINISHED after (?P<cycles>\d+) cycles at (?P<totaltime>.*)")

        for rc in self.postsynthsim_settings['run_configs']:
            name = rc['name']
            report_xml = self.flow_run_dir / rc['report']

            results = self.parse_power_report(report_xml)

            assert results['Design Nets Matched'].startswith('100%')
            assert results['Confidence Level'] == 'High'
            static = results['Device Static (W)']
            if fields['Static'] is None:
                fields['Static'] = static
            else:
                if fields['Static'] != static:
                    _logger.warning(
                        f"Static power for {name} {static} is different from previous test vectors ({fields['Static']})")
                    fields[f'Static:{name}'] = static
            fields[name] = results['Dynamic (W)']

            lwctb_log = self.postsynthsim_flow.flow_run_dir / \
                f'{name}_LWCTB_log.txt'
            with open(lwctb_log) as f:
                match = timing_pat.search(f.read())
                assert match, f"timing pattern not found in the LWC_TB log {lwctb_log}. Make sure simulation has not failed and that you are using the correct version of LWC_TB"
                results['cycles'] = match.group('cycles')
                fields[name + '_cycles'] = results['cycles']
                results['totaltime'] = match.group('totaltime')
            self.results[name] = results

        fields['LUT'] = self.synth_results['lut']
        fields['FF'] = self.synth_results['ff']
        fields['Slice'] = self.synth_results['slice']
        fields['LUT RAM'] = self.synth_results['lut_mem']

        # self.run_path.parent ?
        csv_path = Path.cwd() / f"VivadoPowerLwc_{design_name}_{freq}MHz.csv"
        with open(csv_path, "w") as csv_file:
            writer = csv.DictWriter(csv_file, fields.keys())
            writer.writeheader()
            writer.writerow(fields)

        _logger.info(f"CSV power results written to {csv_path}")
        self.results['success'] = True


class VivadoPowerTimingOnly(SimFlow):
    """
    just trying to reuse VivadoPowerLWc code to redo quick cycle accurate simulation and retrieve missing data
    """
    @classmethod
    def prerequisite_flows(cls, flow_settings, design_settings):

        flow_overrides = {}
        design_overrides = {}

        flow_overrides['clock_period'] = flow_settings.get('clock_period', 13.333)
        flow_overrides['timing_sim'] = False
        flow_overrides['stop_time'] = None
        flow_overrides['saif'] = None
        flow_overrides['vcd'] = None

        tb_settings = design_settings['tb']
        design_name = design_settings['name']
        lwc_settings = design_settings.get('lwc', {})

        power_tvs = flow_settings.get('power_tvs')
        if not power_tvs:
            power_tvs = _default_power_tvs
            algorithms = lwc_settings.get('algorithm')
            if (algorithms and (isinstance(algorithms, list) or isinstance(algorithms, tuple)) and len(algorithms) > 1) or lwc_settings.get('supports_hash'):
                power_tvs.extend(['hash_16', 'hash_1536'])

        lwc_variant = lwc_settings.get('variant')
        if not lwc_variant:
            name_splitted = design_name.split('-')
            assert len(
                name_splitted) > 1, "either specify design.lwc.variant or design.name should be ending with -v\d+"
            lwc_variant = name_splitted[-1]
            assert re.match(
                r'v\d+', lwc_variant), "either specify design.lwc.variant or design.name should be ending with -v\d+"
        power_tvs_root = os.path.join('KAT_GMU', lwc_variant)

        def pow_tv_run_config(tv_sub):
            tv_generics = copy.deepcopy(tb_settings.get('generics', {}))
            tv_generics['G_MAX_FAILURES'] = 1
            tv_generics['G_TEST_MODE'] = 0
            tv_generics['G_FNAME_LOG'] = f'{tv_sub}_LWCTB_log.txt'
            for t in ['pdi', 'sdi', 'do']:
                tv_generics[f'G_FNAME_{t.upper()}'] = DesignSource(
                    os.path.join(power_tvs_root, tv_sub, f'{t}.txt'))
            return dict(generics=tv_generics, saif=None, report=f'{tv_sub}.xml', name=tv_sub)

        flow_overrides['run_configs'] = [
            pow_tv_run_config(tv) for tv in power_tvs]
        design_overrides['tb'] = design_overrides.get('tb', {})
        if 'configuration_specification' not in tb_settings:
            design_overrides['tb']["configuration_specification"] = "LWC_TB_wrapper_conf"


        return {VivadoSim: (flow_overrides, design_overrides)}

    def run(self):
        _logger.info("doing nothing!")

    def parse_reports(self):
        design_name = self.settings.design['name']

        sim_flow = self.completed_dependencies[0]
        sim_flow_settings = sim_flow.settings.flow

        clock_period = sim_flow_settings['clock_period']

        freq = math.floor(1000 / clock_period)
        fields = {'Design': design_name,
                  'Frequency (MHz)': freq, 'Static': None}

        timing_pat = re.compile(
            r"PASS \(0\): SIMULATION FINISHED after (?P<cycles>\d+) cycles at (?P<totaltime>.*)")

        for rc in sim_flow_settings['run_configs']:
            name = rc['name']
            results = {}

            lwctb_log = sim_flow.flow_run_dir / f'{name}_LWCTB_log.txt'
            with open(lwctb_log) as f:
                match = timing_pat.search(f.read())
                assert match, f"timing pattern not found in the LWC_TB log {lwctb_log}. Make sure simulation has not failed and you are using the correct version of LWC_TB"
                results['cycles'] = match.group('cycles')
                fields[name + '_cycles'] = results['cycles']
                results['totaltime'] = match.group('totaltime')
            self.results[name] = results

        csv_path = Path.cwd() / \
            f"VivadoPowerLwcTimingOnly_{design_name}_{freq}MHz.csv"
        with open(csv_path, "w") as csv_file:
            writer = csv.DictWriter(csv_file, fields.keys())
            writer.writeheader()
            writer.writerow(fields)

        _logger.info(f"CSV results written to {csv_path}")
        self.results['success'] = True
