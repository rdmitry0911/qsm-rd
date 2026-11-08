#!/bin/bash
# Rebuild the public line from the local branch without qsm-direct/dist/rdp-hybrid.
# Deterministic: unchanged commits keep their rewritten hash between runs.
set -e
S=<scratch>
R=/home/dima/Projects/q-sunshine
BASE=$(git -C $R rev-parse origin/main)
rm -rf $S/pub.git
git clone -q --bare --no-tags --single-branch --branch qsm-pixel-history-exp213 $R $S/pub.git
git -C $S/pub.git fetch -q $R refs/remotes/origin/main:refs/heads/base
git -C $S/pub.git branch -f pub qsm-pixel-history-exp213
FILTER_BRANCH_SQUELCH_WARNING=1 git -C $S/pub.git filter-branch -f --index-filter 'git rm -r -q --cached --ignore-unmatch qsm-direct/dist/rdp-hybrid' --prune-empty -- $BASE..pub >/dev/null 2>&1
git -C $S/pub.git rev-parse pub
