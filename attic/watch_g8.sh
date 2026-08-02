#!/bin/bash
L=C:/Users/Lenovo/Documents/kimi/workspace/extracted/logs
while true; do
  n=0
  for f in mobile_obb_mining.md locked_assets_round2.md scope_render_data.md; do
    [ -f "$L/$f" ] && n=$((n+1))
  done
  s=$(date +%H:%M:%S)
  echo "$s reports=$n/3 obb=$([ -f $L/mobile_obb_mining.md ] && echo Y || echo -) locked=$([ -f $L/locked_assets_round2.md ] && echo Y || echo -) scope=$([ -f $L/scope_render_data.md ] && echo Y || echo -)"
  [ "$n" = "3" ] && { echo ALL_DONE; break; }
  sleep 240
done
