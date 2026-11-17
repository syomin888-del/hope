#!/usr/bin/env python3
"""ORACLE EVA V1.7.9: single valid JSON on stdout; diagnostics on stderr.

No database schema/model/provider/decision changes. The two exact historical
in-memory adaptations are kept, only their diagnostic output is redirected.
"""
from __future__ import annotations
from contextlib import redirect_stdout
import runpy
import sys
import eva_v177_launcher as prior
import eva_portfolio_filter_v178 as portfolio

def main():
    prior.install_in_memory()
    # The V178 install prints an EVA-only banner. Never mix that with V7 JSON:
    # v174_durable.summarize() parses the *whole* stdout as one JSON document.
    with redirect_stdout(sys.stderr):
        portfolio.install_in_memory()
    sys.argv=[str(prior.APP/'oracle_v5.py'),'once','--db',prior.DB]
    runpy.run_path(str(prior.APP/'oracle_v5.py'),run_name='__main__')

if __name__=='__main__':main()
