#!/usr/bin/env python3
"""V178 EVA-only in-memory historic portfolio eligibility filter + V177 paper filter."""
from __future__ import annotations
import runpy
import sys
from pathlib import Path
import eva_v177_launcher as prior
import eva_portfolio_filter_v178 as portfolio

def main():
    prior.install_in_memory()   # Existing original V5.5 paper source prefilter.
    portfolio.install_in_memory() # New original V5.6 portfolio query guard.
    sys.argv=[str(prior.APP/'oracle_v5.py'),'once','--db',prior.DB]
    runpy.run_path(str(prior.APP/'oracle_v5.py'),run_name='__main__')

if __name__=='__main__':main()
