import os
import sys

# 确保 Tcl/Tk 库路径正确设置
if getattr(sys, '_MEIPASS', ''):
    base = sys._MEIPASS
    tcl_dir = os.path.join(base, 'tcl', 'tcl8.6')
    tk_dir = os.path.join(base, 'tcl', 'tk8.6')
    if os.path.isdir(tcl_dir):
        os.environ['TCL_LIBRARY'] = tcl_dir
    if os.path.isdir(tk_dir):
        os.environ['TK_LIBRARY'] = tk_dir
