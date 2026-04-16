"""
@ScriptName: 123
@Description: 123
@Param: loop_count|int|10|循环次数|True
@Param: sleep_time|int|2|等待秒数|False
"""
__author__ = "tanzhenjie"

from airtest.core.api import *

auto_setup(__file__)

kuaishou_pos = exists(Template(r"tpl1765590200738.png", record_pos=(-0.403, -0.303), resolution=(1600, 2560)))
if kuaishou_pos:
    touch(kuaishou_pos)
    touch(Template(r"tpl1765590537732.png", record_pos=(-0.407, -0.291), resolution=(1600, 2560)))
    sleep(3)
x_pos = exists(Template(r"tpl1765590588955.png", record_pos=(0.421, 0.29), resolution=(1600, 2560)))
if x_pos:
    touch(x_pos)
    sleep(1)
    
make_money_pos = exists(Template(r"tpl1765590647833.png", record_pos=(0.196, 0.699), resolution=(1600, 2560)))
if make_money_pos:
    touch(make_money_pos)
    sleep(2)
    
    
welfare_box_pos = exists(Template(r"tpl1765590812227.png", record_pos=(0.36, 0.528), resolution=(1600, 2560)))
if welfare_box_pos:
    touch(welfare_box_pos)