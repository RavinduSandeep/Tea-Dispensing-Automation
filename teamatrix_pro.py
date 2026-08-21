"""
TeaMatrix Industrial Console v3.1 — UI Layer
=============================================
4-Tab Tkinter HMI:  Dashboard | Station Mgr | Prod Log | Health
All hardware logic lives in teamatrix_backend.py
"""
import sys, threading, csv, os, time, tkinter as tk
from tkinter import ttk, font as tkfont, simpledialog, messagebox
from datetime import datetime

from teamatrix_backend import (
    INGREDIENTS, RECIPES, BOARD_PORTS, CONVEYOR_CH, MIXER_CH,
    CONVEYOR_DUTY, MIXER_DUTY, TECH_PIN, LOG_FILE, MAINT_WARN_HOURS,
    boards, stations, orch, app_ref as _ref,
    init_boards, estop_all, set_motor, save_station_config,
    log_msg, _log_callbacks, _next_oid, next_oid,
    motor_hours, maint_alert, tolerance, motor_off,
    DEAD_ZONE, ZERO_RANGE,
    inventory, refill_station, deduct_stock, low_stock_stations,
    LOW_STOCK_THRESH, save_recipes,
    refresh_station_behavior, ACCEL_STEP,
    DEFAULT_DISPENSE_MODE, DEFAULT_MICRO_THRESHOLD_G,
    DEFAULT_DECEL_FACTOR, DEFAULT_ML_ENABLED,
    DROP_TOLERANCE_G, OPERATOR_DECISION_TIMEOUT_S,
    INGREDIENT_CATALOGUE, load_catalogue, save_catalogue, add_ingredient_to_catalogue,
    DEFAULT_SERVO, _SERVO_RANGES, _coerce_servo,
    MAX_MANUAL_MOTOR_S, MANUAL_TEST_VOLTAGE, MANUAL_DISP_TEST_TARGET_G,
    DEFAULT_SCALE_TOLERANCE_G, DEFAULT_FALL_DELAY_S, DEFAULT_NO_DATA_TIMEOUT_S,
    DEFAULT_TARGET_OFFSET_G, TARGET_OFFSET_MAX_G,
    DEFAULT_SAFE_ANGLE_MIN, DEFAULT_SAFE_ANGLE_MAX, SERVO_MOVE_TIMEOUT_S_DEFAULT,
    SAFE_ANGLE_MIN_LIMIT, SAFE_ANGLE_MAX_LIMIT,
    # Precision-dispensing tuning (now exposed in the Behavior tab so changes
    # actually affect the fill loop — previously these only lived in the
    # JSON file with no UI hook, which is why the Behavior tab "did nothing").
    DEFAULT_FINE_MARGIN_G, DEFAULT_INFLIGHT_COMP_G, DEFAULT_MAX_TOPUP_PULSES,
    DEFAULT_PULSE_MS_LARGE, DEFAULT_PULSE_MS_MEDIUM,
    DEFAULT_PULSE_MS_SMALL, DEFAULT_PULSE_MS_TINY,
    DEFAULT_AUTO_MICRO_TAIL_G, AUTO_MICRO_TAIL_MAX_G,
    # Pre-stop + settle + final-micro stage (operator-spec)
    DEFAULT_FINAL_MICRO_AMOUNT_G, FINAL_MICRO_AMOUNT_MAX_G,
    DEFAULT_SETTLING_DELAY_S,    SETTLING_DELAY_MAX_S,
)
import teamatrix_backend as _bk

# ─── THEME ────────────────────────────────────────────────────────────────────
BG_DARK="#0d0f11"; BG_CARD="#141618"; BG_CARD2="#1a1c20"; BG_PANEL="#111316"
C_GREEN="#00e676"; C_RED="#ff1744"; C_AMBER="#ffab00"; C_BLUE="#448aff"
C_CYAN="#00bcd4"; C_WHITE="#e8eaed"; C_MUTED="#8a8d94"; C_DIM="#3a3d44"
C_BORDER="#1e2128"; C_ACT_BORDER="#00ff00"; C_DIM_BG="#0b0d0b"
FONT_MONO="Courier"; FONT_SANS="Helvetica"
GUI_MS=40

STATUS_COLORS={
    "IDLE":C_MUTED,"TARING":C_BLUE,"PLANNING":C_BLUE,
    "FILLING":C_GREEN,"BRAKING":C_AMBER,"PULSING":C_AMBER,"WAITING":C_BLUE,
    "READY TO DROP":C_AMBER,"DONE":C_GREEN,"STOPPED":C_RED,"ERROR":C_RED,
    "HW_FAULT":C_RED,"POURING":C_GREEN,"TAPPING":C_AMBER,"RESETTING":C_BLUE,
    "RUNNING":C_GREEN,"COMPLETE":C_GREEN,"VERIFYING":C_CYAN,
    "CONVEYOR":C_CYAN,"DROPPING":C_AMBER,"MIXING":"#9c27b0",
    "OFFLINE":C_RED,"FILLING_ALL":C_GREEN,"SOFT_STOP":C_RED,
    "INTERLOCK_FAIL":C_RED,"SOFT-STOP":C_RED,
    "CONVEYOR_PRIMING":C_CYAN,"HOLDING":C_RED,"FAILED":C_RED,"SKIPPED":C_AMBER,
    # New state-machine names (parallel orchestrator)
    "RECIPE_STARTED":C_CYAN,"CONVEYOR_RUNNING":C_CYAN,"DISPENSING":C_GREEN,
    "WEIGHING":C_CYAN,"WEIGHT_VALIDATION":C_AMBER,
    "WAITING_OPERATOR_DECISION":C_RED,
    "SERVO_DROP_TO_CONVEYOR":C_GREEN,"SERVO_EJECT_TO_BASKET":C_AMBER,
    "FINAL_MIX_60_SECONDS":"#9c27b0","FAULT":C_RED,
    "EJECTING":C_AMBER,"EJECTED":C_AMBER,
    # Status names used by the dispensing state-machine
    "SLOWDOWN":"#ffd54f","MICRO_TAIL":"#ffeb3b",
    "MICRO":"#ffeb3b",          # pure-micro phase (tgt ≤ 5g)
    "SETTLING":"#80deea",       # motor-off settle window
    "FINAL_MICRO":"#ffeb3b",    # post-settle micro pulse loop
}

# ─── APPLICATION ──────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        _bk.app_ref=self
        self.title("◈ TEAMATRIX  INDUSTRIAL CONSOLE  v2.1")
        self.configure(bg=BG_DARK); self.geometry("1440x900")
        try: self.attributes("-zoomed",True)
        except Exception: pass

        self._sel=None; self._ws:dict[int,float]={}
        self._tech=False; self._active_sids:set[int]=set()
        self._queue = []
        self._inv_tick=0
        self._stock_vars:dict[int,tk.StringVar]={}   # sid -> StringVar for card stock label
        self._diag_single_sid=None                   # currently selected station on the single-station Diagnostic tab
        self._rec_editing_key=None                   # which recipe (by name) the editor is bound to; None = new

        self._fonts(); self._ui()
        self.after(GUI_MS,self._loop)
        self.protocol("WM_DELETE_WINDOW",self._close)
        threading.Thread(target=init_boards,daemon=True).start()

    # ── Fonts ────────────────────────────────────────────────────────────────
    def _fonts(self):
        self.fT  = tkfont.Font(family=FONT_SANS, size=14, weight="bold")
        self.fXL = tkfont.Font(family=FONT_MONO, size=26, weight="bold")
        self.fLG = tkfont.Font(family=FONT_MONO, size=14, weight="bold")
        self.fSM = tkfont.Font(family=FONT_MONO, size=12)            # was 10
        self.fLB = tkfont.Font(family=FONT_SANS, size=13)            # was 11
        self.fBT = tkfont.Font(family=FONT_SANS, size=11, weight="bold")  # was 9
        self.fBG = tkfont.Font(family=FONT_MONO, size=10, weight="bold")  # was 8
        self.fMD = tkfont.Font(family=FONT_MONO, size=11)            # was 9
        self.fING= tkfont.Font(family=FONT_SANS, size=13, weight="bold")  # was 11

    # ── Top bar + Notebook ───────────────────────────────────────────────────
    def _ui(self):
        top=tk.Frame(self,bg="#0a0c0e",height=46)
        top.pack(side=tk.TOP,fill=tk.X); top.pack_propagate(False)
        tk.Label(top,text="◈  TEAMATRIX v3.1",font=self.fT,bg="#0a0c0e",fg=C_GREEN
                 ).pack(side=tk.LEFT,padx=12,pady=9)
        self.lbl_oid=tk.Label(top,text="ORDER —",font=self.fSM,bg="#0a0c0e",fg=C_CYAN)
        self.lbl_oid.pack(side=tk.LEFT,padx=20)
        self.lbl_clk=tk.Label(top,text="",font=self.fSM,bg="#0a0c0e",fg=C_MUTED)
        self.lbl_clk.pack(side=tk.RIGHT,padx=12)
        # Board dots + latency
        self.bdots={}; self.blat={}
        for bid in [4,3,2,1]:
            l=tk.Label(top,text="—ms",font=self.fBG,bg="#0a0c0e",fg=C_DIM)
            l.pack(side=tk.RIGHT,padx=1,pady=13); self.blat[bid]=l
            d=tk.Label(top,text=f"B{bid}",font=self.fBG,bg="#0a0c0e",fg=C_RED)
            d.pack(side=tk.RIGHT,padx=3,pady=13); self.bdots[bid]=d
        tk.Button(top,text="⟳ Refresh Connection",font=self.fBT,bg=BG_CARD2,fg=C_WHITE,
                  relief=tk.FLAT,padx=8,pady=4,command=self._refresh_boards
                  ).pack(side=tk.RIGHT,padx=10,pady=8)
        tk.Button(top,text="⬛ E-STOP",font=self.fBT,bg=C_RED,fg="#fff",relief=tk.FLAT,
                  padx=10,pady=4,activebackground="#cc0020",
                  command=self._estop).pack(side=tk.LEFT,padx=12,pady=8)
        # Global tech-mode badge + toggle (visible from every tab)
        self.btn_tech=tk.Button(top,text="🔒 LOCKED",font=self.fBT,
                                bg=BG_CARD2,fg=C_RED,relief=tk.FLAT,
                                padx=10,pady=4,
                                command=self._toggle_tech)
        self.btn_tech.pack(side=tk.LEFT,padx=8,pady=8)
        # Per-tab "lbl_tech" label is kept for visibility but mirrors btn_tech.
        self.lbl_tech=self.btn_tech

        # Notebook — bigger, easier-to-read tab labels (11pt bold, [20,8] padding)
        sty=ttk.Style(); sty.theme_use("clam")
        sty.configure("TNotebook",background=BG_DARK,borderwidth=0)
        sty.configure("TNotebook.Tab",background=BG_CARD2,foreground=C_MUTED,
                      padding=[20,8],font=(FONT_SANS,11,"bold"))
        sty.map("TNotebook.Tab",background=[("selected",BG_PANEL)],
                foreground=[("selected",C_GREEN)])
        self.nb=ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH,expand=True,padx=4,pady=(2,4))

        # Operator zone (left): Dashboard first. Standard styling.
        # UI FIX:
        #  - "RECIPES" tab moved into the Technician zone and renamed
        #    "Recipe Change" so operators cannot edit/delete recipes
        #    without a technician password.
        #  - "STATION MGR" tab removed entirely — no longer needed.
        #  - "PROD LOG" stays in the operator zone and is given more
        #    real-estate (see _tab_log) so production records are always
        #    visible to the operator.
        t_dash=tk.Frame(self.nb,bg=BG_DARK);  self.nb.add(t_dash, text="◈  DASHBOARD");  self._tab_dash(t_dash)
        t_inv =tk.Frame(self.nb,bg=BG_DARK);  self.nb.add(t_inv,  text="📦  INVENTORY"); self._tab_inventory(t_inv)
        t_log =tk.Frame(self.nb,bg=BG_DARK);  self.nb.add(t_log,  text="📋  PROD LOG");  self._tab_log(t_log)
        t_hlth=tk.Frame(self.nb,bg=BG_DARK);  self.nb.add(t_hlth, text="🔬  HEALTH");    self._tab_health(t_hlth)
        # Disabled visual divider — boundary between operator and tech zones,
        # amber-tinted to signal "everything past this point requires tech mode".
        t_div =tk.Frame(self.nb,bg=BG_DARK);  self.nb.add(t_div,  text="◤  TECHNICIAN  ◥")
        self.nb.tab(t_div,state="disabled")
        # Technician zone (right): Behavior, Recipe Change, Diagnostic. All
        # password-gated. We track these tab widgets so _refresh_tech_ui can
        # rewrite their headers with a 🔒 / 🔓 prefix that flips with tech state,
        # and so we can block tab selection when NOT in tech mode.
        t_beh =tk.Frame(self.nb,bg=BG_PANEL); self.nb.add(t_beh,  text="🔒  🎚  BEHAVIOR");      self._tab_behavior(t_beh)
        t_rec =tk.Frame(self.nb,bg=BG_PANEL); self.nb.add(t_rec,  text="🔒  🍵  RECIPE CHANGE"); self._tab_recipes(t_rec)
        t_diag=tk.Frame(self.nb,bg=BG_PANEL); self.nb.add(t_diag, text="🔒  🛠  DIAGNOSTIC");    self._tab_diagnostic(t_diag)
        # Tech-tab list: (frame, base text without lock prefix). _refresh_tech_ui
        # rewrites the header text on every tech-mode toggle.
        self._tech_tabs=[
            (t_beh, "🎚  BEHAVIOR"),
            (t_rec, "🍵  RECIPE CHANGE"),
            (t_diag,"🛠  DIAGNOSTIC"),
        ]
        # Block selection of tech-gated tabs when not unlocked — bounces user
        # back to the previously-selected operator tab so the password is
        # actually required before they can see the editor.
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._last_safe_tab=t_dash
        # Track the Production Log tab frame so we can auto-refresh on focus
        self._tab_log_frame=t_log
        self._log_strip()

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 0 — DASHBOARD
    # ══════════════════════════════════════════════════════════════════════════
    def _tab_dash(self,p):
        body=tk.Frame(p,bg=BG_DARK); body.pack(fill=tk.BOTH,expand=True)
        left=tk.Frame(body,bg=BG_PANEL,width=350)
        left.pack(side=tk.LEFT,fill=tk.Y,padx=(5,2),pady=5); left.pack_propagate(False)
        self._left(left)
        right=tk.Frame(body,bg=BG_DARK)
        right.pack(side=tk.LEFT,fill=tk.BOTH,expand=True,padx=(2,5),pady=5)
        self._grid(right)

    def _left(self,p):
        self._sec(p,"RECIPE")
        lbf=tk.Frame(p,bg=BG_CARD,highlightbackground=C_BORDER,highlightthickness=1)
        lbf.pack(fill=tk.X,padx=7,pady=(0,3))
        self.rlb=tk.Listbox(lbf,font=self.fING,bg=BG_CARD,fg=C_WHITE,
                            selectbackground=C_GREEN,selectforeground="#000",
                            activestyle="none",bd=0,relief=tk.FLAT,
                            highlightthickness=0,height=12)
        for n in RECIPES: self.rlb.insert(tk.END,n)
        self.rlb.pack(fill=tk.X,padx=3,pady=3)
        self.rlb.bind("<<ListboxSelect>>",self._sel_recipe)

        self._sec(p,"INGREDIENTS")
        self.ing=tk.Frame(p,bg=BG_CARD,highlightbackground=C_BORDER,highlightthickness=1)
        self.ing.pack(fill=tk.X,padx=7,pady=(0,3)); self._ing([])

        self._sec(p,"BATCH WEIGHT")
        br=tk.Frame(p,bg=BG_PANEL); br.pack(fill=tk.X,padx=7)
        for g in [50,80,100]:
            tk.Button(br,text=f"{g}g",font=self.fBT,bg=BG_CARD2,fg=C_WHITE,
                      relief=tk.FLAT,padx=7,pady=4,width=4,activebackground=C_BLUE,
                      command=lambda v=g:self._scale(v)).pack(side=tk.LEFT,padx=2,pady=3)
        cr=tk.Frame(p,bg=BG_PANEL); cr.pack(fill=tk.X,padx=7,pady=(2,3))
        tk.Label(cr,text="Custom:",font=self.fLB,bg=BG_PANEL,fg=C_MUTED).pack(side=tk.LEFT)
        self.e_cust=tk.Entry(cr,font=self.fSM,bg=BG_CARD,fg=C_GREEN,
                             insertbackground=C_GREEN,relief=tk.FLAT,width=6,justify=tk.CENTER)
        self.e_cust.pack(side=tk.LEFT,padx=3)
        tk.Button(cr,text="SET",font=self.fBG,bg=C_BLUE,fg="#fff",relief=tk.FLAT,
                  padx=5,pady=2,command=self._custom).pack(side=tk.LEFT)
        self.lbl_tot=tk.Label(p,text="Total: — g",font=self.fSM,bg=BG_PANEL,fg=C_MUTED)
        self.lbl_tot.pack(padx=7,pady=(0,4))

        tk.Button(p,text="➕ ADD TO QUEUE",font=self.fBT,bg=C_BLUE,fg="#fff",
                  relief=tk.FLAT,padx=10,pady=8,activebackground="#2962ff",
                  command=self._add_queue).pack(fill=tk.X,padx=7,pady=3)

        self._sec(p,"ORDER QUEUE")
        # Order queue with both vertical AND horizontal scrollbars so long
        # recipe names can never hide the Edit/Delete buttons. The Canvas
        # window is created at natural width (no x-binding) so cards keep
        # their natural width — when the panel is narrow, the horizontal
        # scrollbar lets the user reach the right-edge buttons.
        qouter=tk.Frame(p,bg=BG_CARD2,highlightbackground=C_BORDER,highlightthickness=1)
        qouter.pack(fill=tk.X,padx=7,pady=(0,3))
        qcv=tk.Canvas(qouter,bg=BG_CARD2,highlightthickness=0,height=160)
        qsb =ttk.Scrollbar(qouter,orient=tk.VERTICAL,  command=qcv.yview)
        qhsb=ttk.Scrollbar(qouter,orient=tk.HORIZONTAL,command=qcv.xview)
        self.q_inner=tk.Frame(qcv,bg=BG_CARD2)
        self.q_inner.bind("<Configure>",lambda e:qcv.configure(scrollregion=qcv.bbox("all")))
        qcv.create_window((0,0),window=self.q_inner,anchor=tk.NW)
        qcv.configure(yscrollcommand=qsb.set,xscrollcommand=qhsb.set)
        # Grid layout: Canvas (0,0), vertical sb (0,1), horizontal sb (1,0).
        qcv.grid(row=0,column=0,sticky="nsew")
        qsb.grid(row=0,column=1,sticky="ns")
        qhsb.grid(row=1,column=0,sticky="ew")
        qouter.rowconfigure(0,weight=1)
        qouter.columnconfigure(0,weight=1)
        # Mouse-wheel scrolls the queue vertically when the cursor is over it.
        def _q_wheel(e):
            d=0
            if getattr(e,"num",None)==4: d=-1
            elif getattr(e,"num",None)==5: d=1
            elif getattr(e,"delta",0)>0: d=-1
            elif getattr(e,"delta",0)<0: d=1
            if d: qcv.yview_scroll(d,"units")
        for w in (qcv,self.q_inner):
            w.bind("<MouseWheel>",_q_wheel)
            w.bind("<Button-4>",_q_wheel)
            w.bind("<Button-5>",_q_wheel)

        self._sec(p,"GLOBAL CONTROLS")
        # UI FIX: Start / Pause / Stop on one clean horizontal row.
        # The bottom E-STOP button was removed by request — the real
        # emergency-stop logic still lives in `_estop` / `orch.abort` /
        # backend `estop_all`, and the top-bar ⬛ E-STOP button still
        # invokes it. Removing the duplicate here avoids accidental
        # double-actions and reduces clutter.
        gr=tk.Frame(p,bg=BG_PANEL); gr.pack(fill=tk.X,padx=7,pady=2)
        gr.columnconfigure(0,weight=1)
        gr.columnconfigure(1,weight=1)
        gr.columnconfigure(2,weight=1)
        tk.Button(gr,text="▶ START",font=self.fBT,bg=C_GREEN,fg="#000",relief=tk.FLAT,
                  pady=4,activebackground="#00b85c",command=self._start_queue
                  ).grid(row=0,column=0,padx=2,pady=2,sticky="ew")
        # PAUSE / RESUME are both useful — keep both, but compress to single row.
        tk.Button(gr,text="⏸ PAUSE",font=self.fBT,bg=C_AMBER,fg="#000",relief=tk.FLAT,
                  pady=4,command=orch.pause
                  ).grid(row=0,column=1,padx=2,pady=2,sticky="ew")
        tk.Button(gr,text="■ STOP",font=self.fBT,bg=C_RED,fg="#fff",relief=tk.FLAT,
                  pady=4,command=self._estop
                  ).grid(row=0,column=2,padx=2,pady=2,sticky="ew")
        # Refresh / Reset — re-renders queue, refreshes station cards & inventory.
        # Asks for confirmation if anything is actively running so we don't
        # surprise the operator. Never modifies recipes / station_config / inventory.
        gr2=tk.Frame(p,bg=BG_PANEL); gr2.pack(fill=tk.X,padx=7,pady=2)
        gr2.columnconfigure(0,weight=1); gr2.columnconfigure(1,weight=1)
        tk.Button(gr2,text="⏵ RESUME",font=self.fBT,bg=C_CYAN,fg="#000",relief=tk.FLAT,
                  pady=4,command=orch.resume
                  ).grid(row=0,column=0,padx=2,pady=2,sticky="ew")
        tk.Button(gr2,text="🔄 REFRESH",font=self.fBT,bg=C_BLUE,fg="#fff",relief=tk.FLAT,
                  pady=4,command=self._dashboard_refresh
                  ).grid(row=0,column=1,padx=2,pady=2,sticky="ew")

        self._sec(p,"CONVEYOR / MIXER")
        row=tk.Frame(p,bg=BG_PANEL); row.pack(fill=tk.X,padx=14,pady=2)
        self.lbl_cv=tk.Label(row,text="CV:IDLE ",font=self.fBG,bg=BG_PANEL,fg=C_MUTED)
        self.lbl_cv.pack(side=tk.LEFT)
        self.lbl_mx=tk.Label(row,text="MX:IDLE",font=self.fBG,bg=BG_PANEL,fg=C_MUTED)
        self.lbl_mx.pack(side=tk.LEFT)
        cbr=tk.Frame(p,bg=BG_PANEL); cbr.pack(fill=tk.X,padx=14,pady=(0,3))
        def _cv_start():
            import teamatrix_backend as _bk
            set_motor(4,CONVEYOR_CH,CONVEYOR_DUTY)
            _bk.conveyor_running=True
            self.set_cv("RUNNING")
            log_msg("Manual CV START","info")
        def _cv_stop():
            import teamatrix_backend as _bk
            set_motor(4,CONVEYOR_CH,0)
            _bk.conveyor_running=False
            self.set_cv("IDLE")
            log_msg("Manual CV STOP","info")
        def _mx_start():
            set_motor(4,MIXER_CH,MIXER_DUTY)
            self.set_mx("RUNNING")
            log_msg("Manual MX START","info")
        def _mx_stop():
            set_motor(4,MIXER_CH,0)
            self.set_mx("IDLE")
            log_msg("Manual MX STOP","info")
        for lbl,cmd,fg in [("▶ CV",_cv_start,C_GREEN),("■ CV",_cv_stop,C_RED),
                           ("▶ MX",_mx_start,"#9c27b0"),("■ MX",_mx_stop,C_RED)]:
            tk.Button(cbr,text=lbl,font=self.fBG,bg=BG_CARD2,fg=fg,relief=tk.FLAT,
                      padx=7,pady=3,command=cmd).pack(side=tk.LEFT,padx=2,pady=2)
        self._sec(p,"STATE")
        self.lbl_state=tk.Label(p,text="IDLE",font=self.fLG,bg=BG_PANEL,fg=C_MUTED)
        self.lbl_state.pack(anchor=tk.W,padx=14)

    def _sec(self,p,t):
        f=tk.Frame(p,bg=BG_PANEL); f.pack(fill=tk.X,padx=7,pady=(6,1))
        tk.Label(f,text=t,font=self.fBG,bg=BG_PANEL,fg=C_DIM).pack(side=tk.LEFT)
        tk.Frame(f,bg=C_BORDER,height=1).pack(side=tk.LEFT,fill=tk.X,expand=True,padx=(4,0))

    def _ing(self,items):
        for w in self.ing.winfo_children(): w.destroy()
        if not items:
            tk.Label(self.ing,text="No recipe selected",font=self.fLB,
                     bg=BG_CARD,fg=C_DIM).pack(padx=7,pady=5); return
        for i,(sid,tgt) in enumerate(items):
            bg2=BG_CARD if i%2==0 else BG_CARD2
            row=tk.Frame(self.ing,bg=bg2); row.pack(fill=tk.X)
            tk.Label(row,text=f"S{sid:02d}",font=self.fBG,bg=bg2,fg=C_BLUE,width=4
                     ).pack(side=tk.LEFT,padx=3,pady=2)
            tk.Label(row,text=INGREDIENTS[sid]["label"],font=self.fING,bg=bg2,fg=C_WHITE
                     ).pack(side=tk.LEFT)
            tol=tolerance(tgt)
            tk.Label(row,text=f"{tgt:.0f}g±{tol:.1f}",font=self.fBG,bg=bg2,fg=C_GREEN
                     ).pack(side=tk.RIGHT,padx=5)

    # ── 4×4 Grid ─────────────────────────────────────────────────────────────
    def _grid(self,p):
        for c in range(4): p.columnconfigure(c,weight=1,uniform="c")
        for r in range(4): p.rowconfigure(r,weight=1,uniform="r")
        self._cards={}
        for sid in range(1,14):
            r,c=divmod(sid-1,4)
            self._card(p,sid,r,c)
        self._cv_card(p,3,1); self._mx_card(p,3,2)

    def _card(self,p,sid,row,col):
        st=stations[sid]
        card=tk.Frame(p,bg=BG_CARD,highlightbackground=C_BORDER,highlightthickness=1)
        card.grid(row=row,column=col,sticky="nsew",padx=2,pady=2)
        st.card_frame=card; self._cards[sid]=card
        hdr=tk.Frame(card,bg=BG_CARD2); hdr.pack(fill=tk.X)
        tk.Label(hdr,text=f"S{sid:02d}",font=self.fBG,bg=BG_CARD2,fg=C_DIM
                 ).pack(side=tk.LEFT,padx=5,pady=3)
        st.lbl_s=tk.Label(hdr,text="IDLE",font=self.fBG,bg=BG_CARD2,fg=C_MUTED)
        st.lbl_s.pack(side=tk.RIGHT,padx=5)
        tk.Label(card,text=st.label,font=self.fING,bg=BG_CARD,fg=C_BLUE,wraplength=200
                 ).pack(anchor=tk.W,padx=5,pady=(2,0))
        # Stock remaining label — shows live container level
        sv=tk.StringVar(value="Stock: —")
        self._stock_vars[sid]=sv
        tk.Label(card,textvariable=sv,font=self.fBG,bg=BG_CARD,fg=C_AMBER
                 ).pack(anchor=tk.W,padx=5)
        st.lbl_w=tk.Label(card,text="0.00",font=self.fXL,bg=BG_CARD,fg=C_GREEN)
        st.lbl_w.pack(padx=5)
        tr=tk.Frame(card,bg=BG_CARD); tr.pack(fill=tk.X,padx=5)
        tk.Label(tr,text="tgt g:",font=self.fLB,bg=BG_CARD,fg=C_DIM).pack(side=tk.LEFT)
        st.e_tgt=tk.Entry(tr,font=self.fSM,bg=BG_CARD2,fg=C_GREEN,
                          insertbackground=C_GREEN,relief=tk.FLAT,width=6,justify=tk.CENTER)
        st.e_tgt.insert(0,"0.0"); st.e_tgt.pack(side=tk.LEFT,padx=3)
        try:
            sty=ttk.Style()
            sty.configure(f"S{sid}.Horizontal.TProgressbar",
                          troughcolor=BG_CARD2,background=C_GREEN,borderwidth=0,thickness=4)
            st.prog=tk.DoubleVar(value=0.0)
            ttk.Progressbar(card,variable=st.prog,maximum=100,orient=tk.HORIZONTAL,
                            style=f"S{sid}.Horizontal.TProgressbar"
                            ).pack(fill=tk.X,padx=5,pady=(2,1))
        except Exception: st.prog=None
        br=tk.Frame(card,bg=BG_CARD); br.pack(fill=tk.X,padx=5,pady=(0,3))
        st.b_start=None; st.b_stop=None; st.b_drop=None
        tk.Button(br,text="⊙ TARE",font=self.fBG,bg=BG_CARD2,fg=C_MUTED,relief=tk.FLAT,
                  padx=5,pady=2,
                  command=lambda s=st:threading.Thread(target=s.tare,daemon=True).start()
                  ).pack(side=tk.LEFT)
        tk.Button(br,text="EJ",font=self.fBG,bg=BG_CARD2,fg=C_RED,relief=tk.FLAT,
                  padx=5,pady=2,command=st.eject).pack(side=tk.RIGHT)

    def _cv_card(self,p,row,col):
        card=tk.Frame(p,bg=BG_CARD,highlightbackground=C_BORDER,highlightthickness=1)
        card.grid(row=row,column=col,sticky="nsew",padx=2,pady=2)
        tk.Label(card,text="CONVEYOR",font=self.fBG,bg=BG_CARD,fg=C_DIM).pack(pady=(8,2))
        self.lbl_cv2=tk.Label(card,text="IDLE",font=self.fLG,bg=BG_CARD,fg=C_MUTED)
        self.lbl_cv2.pack()

    def _mx_card(self,p,row,col):
        card=tk.Frame(p,bg=BG_CARD,highlightbackground=C_BORDER,highlightthickness=1)
        card.grid(row=row,column=col,columnspan=2,sticky="nsew",padx=2,pady=2)
        tk.Label(card,text="MIXER",font=self.fBG,bg=BG_CARD,fg=C_DIM).pack(pady=(8,2))
        self.lbl_mx2=tk.Label(card,text="IDLE",font=self.fLG,bg=BG_CARD,fg=C_MUTED)
        self.lbl_mx2.pack()

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 1 — STATION MANAGER
    # ══════════════════════════════════════════════════════════════════════════
    def _tab_mgr(self,p):
        hdr=tk.Frame(p,bg=BG_PANEL); hdr.pack(fill=tk.X,padx=10,pady=8)
        tk.Label(hdr,text="Station Manager",font=self.fLG,bg=BG_PANEL,fg=C_WHITE
                 ).pack(side=tk.LEFT)
        tk.Label(hdr,text="reassign which ingredient lives in each station — IDs and channels stay fixed",
                 font=self.fSM,bg=BG_PANEL,fg=C_MUTED
                 ).pack(side=tk.LEFT,padx=(10,0),pady=(4,0))
        # Tech toggle now lives in the global top header — no per-tab button.
        tk.Button(hdr,text="+ Add Ingredient",font=self.fBT,bg=C_BLUE,fg="#fff",
                  relief=tk.FLAT,padx=8,pady=3,
                  command=self._add_ingredient).pack(side=tk.RIGHT,padx=5)
        tk.Button(hdr,text="💾 Save All",font=self.fBT,bg=C_AMBER,fg="#000",
                  relief=tk.FLAT,padx=8,pady=3,
                  command=self._save_all).pack(side=tk.RIGHT,padx=5)

        # Style for the ingredient combobox so it matches the dark theme
        sty=ttk.Style(self)
        sty.configure("Mgr.TCombobox",fieldbackground=BG_DARK,background=BG_CARD2,
                      foreground=C_WHITE,arrowcolor=C_GREEN,bordercolor=BG_CARD2,
                      lightcolor=BG_CARD2,darkcolor=BG_CARD2)
        sty.map("Mgr.TCombobox",fieldbackground=[("readonly",BG_DARK)],
                foreground=[("readonly",C_WHITE)])

        # Column headers
        th=tk.Frame(p,bg=BG_CARD2); th.pack(fill=tk.X,padx=10,pady=(0,1))
        for i,(h,w) in enumerate([("St",4),("Current Ingredient",22),("Bd",4),("Ch",4),
                                  ("Reassign To",24),("",6)]):
            tk.Label(th,text=h,font=self.fBG,bg=BG_CARD2,fg=C_DIM,width=w,anchor=tk.W
                     ).grid(row=0,column=i,padx=4,pady=3,sticky=tk.W)

        # Scrollable rows
        sf=tk.Frame(p,bg=BG_DARK); sf.pack(fill=tk.BOTH,expand=True,padx=10,pady=(0,8))
        cv=tk.Canvas(sf,bg=BG_DARK,highlightthickness=0)
        sb=ttk.Scrollbar(sf,orient=tk.VERTICAL,command=cv.yview)
        inner=tk.Frame(cv,bg=BG_DARK)
        inner.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0,0),window=inner,anchor=tk.NW)
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side=tk.LEFT,fill=tk.BOTH,expand=True); sb.pack(side=tk.RIGHT,fill=tk.Y)

        self._ren={}
        catalogue=load_catalogue()
        for i,sid in enumerate(INGREDIENTS):
            info=INGREDIENTS[sid]
            bg2=BG_CARD if i%2==0 else BG_CARD2
            row_f=tk.Frame(inner,bg=bg2); row_f.pack(fill=tk.X,pady=1)
            tk.Label(row_f,text=f"S{sid:02d}",font=self.fBG,bg=bg2,fg=C_BLUE,width=4,anchor=tk.W
                     ).grid(row=0,column=0,padx=4,pady=4,sticky=tk.W)
            cur_var=tk.StringVar(value=info["label"])
            tk.Label(row_f,textvariable=cur_var,font=self.fLB,bg=bg2,fg=C_WHITE,
                     width=22,anchor=tk.W
                     ).grid(row=0,column=1,padx=4,sticky=tk.W)
            tk.Label(row_f,text=f"Bd{info['board']}",font=self.fBG,bg=bg2,fg=C_DIM,
                     width=4,anchor=tk.W
                     ).grid(row=0,column=2,padx=4,sticky=tk.W)
            tk.Label(row_f,text=f"Ch{info['ch']}",font=self.fBG,bg=bg2,fg=C_DIM,
                     width=4,anchor=tk.W
                     ).grid(row=0,column=3,padx=4,sticky=tk.W)
            sel_var=tk.StringVar(value=info["label"])
            cb=ttk.Combobox(row_f,textvariable=sel_var,values=catalogue,
                            state="readonly",width=22,style="Mgr.TCombobox",
                            font=self.fSM)
            cb.grid(row=0,column=4,padx=4,pady=3,sticky=tk.W)
            self._ren[sid]={"var":cur_var,"sel":sel_var,"combo":cb}
            tk.Button(row_f,text="SAVE",font=self.fBG,bg=C_AMBER,fg="#000",relief=tk.FLAT,
                      padx=10,pady=2,
                      command=lambda s=sid:self._do_rename(s)
                      ).grid(row=0,column=5,padx=4)

    def _toggle_tech(self):
        if self._tech:
            self._tech=False
            self._refresh_tech_ui()
            log_msg("Technician mode OFF","info")
        else:
            pin=simpledialog.askstring("Technician Mode","Enter PIN:",show="*",parent=self)
            if pin==TECH_PIN:
                self._tech=True
                self._refresh_tech_ui()
                log_msg("Technician mode ACTIVE","warn")
            else:
                log_msg("Incorrect PIN","error")

    def _on_tab_changed(self,_event=None):
        """Gate tech-zone tabs behind the technician PIN.
        Recipe Change in particular must require the password BEFORE the
        editor is visible. If the operator clicks one of the tech tabs while
        NOT in tech mode, we prompt; on success we keep them there, on
        failure (or cancel) we bounce back to the last safe operator tab.
        """
        try:
            cur=self.nb.select()
            cur_w=self.nb.nametowidget(cur)
        except Exception:
            return
        # Identify whether the selected tab is one of the tech-gated frames
        tech_frames={f for f,_ in getattr(self,"_tech_tabs",[])}
        if cur_w in tech_frames:
            if not self._tech:
                pin=simpledialog.askstring(
                    "Technician PIN Required",
                    "This tab is restricted.\nEnter the technician PIN:",
                    show="*",parent=self)
                if pin==TECH_PIN:
                    self._tech=True
                    self._refresh_tech_ui()
                    log_msg("Technician mode ACTIVE (tab access)","warn")
                else:
                    log_msg("Tech tab access denied — wrong/blank PIN","error")
                    try:
                        self.nb.select(self._last_safe_tab)
                    except Exception:
                        pass
                    return
        else:
            # Remember the last operator-zone tab so we can bounce back to it
            self._last_safe_tab=cur_w
        # Auto-refresh the Production Log when the operator opens its tab so
        # newly-written rows appear without having to click ⟳ first.
        if getattr(self,"_tab_log_frame",None) is cur_w:
            try: self._refresh_log()
            except Exception: pass

    def _refresh_tech_ui(self):
        """Update the global tech badge + propagate to any tech-gated widgets.
        Also rewrites tech-tab headers so password-gated tabs visually flip
        between 🔒 (locked, amber) and 🔓 (unlocked) — making it obvious
        which tabs require technician access vs. normal operator tabs."""
        on=self._tech
        if hasattr(self,"btn_tech"):
            self.btn_tech.config(text="🔓 TECH MODE" if on else "🔒 LOCKED",
                                 fg=C_AMBER if on else C_RED)
        # Tech-zone tab headers: lock-prefix flips with tech state. Normal
        # operator tabs are NOT touched, preserving the visual distinction
        # called out by the design ("only password-required tabs get this
        # special header style").
        if hasattr(self,"_tech_tabs") and hasattr(self,"nb"):
            prefix="🔓 " if on else "🔒 "
            for frame,base in self._tech_tabs:
                try: self.nb.tab(frame,text=f"{prefix} {base}")
                except Exception: pass
        # Re-evaluate enabled/disabled state for any tech-gated widgets
        if hasattr(self,"_apply_tech_gates"):
            self._apply_tech_gates()

    def _do_rename(self,sid):
        """Reassign the ingredient at station `sid` to whatever the combobox selected."""
        if not self._tech: log_msg("Technician Mode required","error"); return
        d=self._ren[sid]
        new=d["sel"].get().strip()
        if not new: log_msg(f"S{sid}: ingredient empty","error"); return
        INGREDIENTS[sid]["label"]=new
        INGREDIENTS[sid]["ingredient_name"]=new
        stations[sid].label=new
        d["var"].set(new)
        save_station_config(INGREDIENTS)
        # Live-update other tabs
        refresh_station_behavior()
        if hasattr(self,"_inv_rows") and sid in self._inv_rows:
            self._inv_refresh()
        log_msg(f"S{sid:02d} reassigned → {new}","ok")

    def _save_all(self):
        if not self._tech: log_msg("Technician Mode required","error"); return
        changed=0
        for sid,d in self._ren.items():
            new=d["sel"].get().strip()
            if new and new!=INGREDIENTS[sid].get("label"):
                INGREDIENTS[sid]["label"]=new
                INGREDIENTS[sid]["ingredient_name"]=new
                stations[sid].label=new
                d["var"].set(new)
                changed+=1
        save_station_config(INGREDIENTS)
        refresh_station_behavior()
        if hasattr(self,"_inv_rows"):
            self._inv_refresh()
        log_msg(f"Saved {changed} station reassignment(s)","ok" if changed else "info")

    def _add_ingredient(self):
        """Add a new ingredient to the catalogue (gated by Tech Mode).
        Refreshes every Combobox in the Station Manager."""
        if not self._tech:
            log_msg("Technician Mode required to add ingredient","error"); return
        name=simpledialog.askstring("Add Ingredient","New ingredient name:",parent=self)
        if not name or not name.strip():
            return
        catalogue=add_ingredient_to_catalogue(name.strip())
        # Refresh all comboboxes in the Mgr tab
        for d in self._ren.values():
            d["combo"]["values"]=catalogue
        log_msg(f"Ingredient '{name.strip()}' added to catalogue ({len(catalogue)} total)","ok")

    # ══════════════════════════════════════════════════════════════════════════
    # TAB — STATION BEHAVIOR (per-station dispense + servo sequence editor)
    # Two-pane layout: station list (left) + detail editor (right).
    # All inputs disabled when not in Technician Mode.
    # ══════════════════════════════════════════════════════════════════════════
    def _tab_behavior(self,p):
        # Header
        hdr=tk.Frame(p,bg=BG_PANEL); hdr.pack(fill=tk.X,padx=10,pady=8)
        tk.Label(hdr,text="Station Behavior",font=self.fLG,bg=BG_PANEL,fg=C_WHITE
                 ).pack(side=tk.LEFT)
        tk.Label(hdr,text="per-station dispense + servo sequence — ML model preserved",
                 font=self.fSM,bg=BG_PANEL,fg=C_MUTED
                 ).pack(side=tk.LEFT,padx=(10,0),pady=(4,0))
        tk.Button(hdr,text="💾 Save",font=self.fBT,bg=C_AMBER,fg="#000",
                  relief=tk.FLAT,padx=12,pady=4,
                  command=self._beh_save_current).pack(side=tk.RIGHT,padx=5)
        tk.Button(hdr,text="↺ Reload",font=self.fBT,bg=BG_CARD2,fg=C_WHITE,
                  relief=tk.FLAT,padx=12,pady=4,
                  command=self._beh_reload).pack(side=tk.RIGHT,padx=5)
        tk.Button(hdr,text="⤺ Reset to Defaults",font=self.fBT,bg=BG_CARD2,fg=C_MUTED,
                  relief=tk.FLAT,padx=12,pady=4,
                  command=self._beh_reset_defaults).pack(side=tk.RIGHT,padx=5)

        # Combobox style (reused across tabs)
        sty=ttk.Style(self)
        sty.configure("Beh.TCombobox",fieldbackground=BG_DARK,background=BG_CARD2,
                      foreground=C_WHITE,arrowcolor=C_GREEN,bordercolor=BG_CARD2,
                      lightcolor=BG_CARD2,darkcolor=BG_CARD2)
        sty.map("Beh.TCombobox",fieldbackground=[("readonly",BG_DARK)],
                foreground=[("readonly",C_WHITE)])

        # ── Body: 2-pane layout ──────────────────────────────────────────────
        body=tk.Frame(p,bg=BG_PANEL); body.pack(fill=tk.BOTH,expand=True,padx=10,pady=(0,8))

        # Left pane: station list
        left=tk.Frame(body,bg=BG_CARD,width=200); left.pack(side=tk.LEFT,fill=tk.Y,padx=(0,8))
        left.pack_propagate(False)
        tk.Label(left,text="STATIONS",font=self.fBG,bg=BG_CARD,fg=C_DIM,anchor=tk.W
                 ).pack(fill=tk.X,padx=8,pady=(8,4))
        list_wrap=tk.Frame(left,bg=BG_CARD); list_wrap.pack(fill=tk.BOTH,expand=True,padx=8,pady=(0,8))
        self._beh_list=tk.Listbox(list_wrap,bg=BG_DARK,fg=C_WHITE,
                                  selectbackground=C_GREEN,selectforeground="#000",
                                  font=self.fLB,activestyle="none",
                                  highlightthickness=0,relief=tk.FLAT,bd=0)
        sb=ttk.Scrollbar(list_wrap,orient=tk.VERTICAL,command=self._beh_list.yview)
        self._beh_list.configure(yscrollcommand=sb.set)
        self._beh_list.pack(side=tk.LEFT,fill=tk.BOTH,expand=True)
        sb.pack(side=tk.RIGHT,fill=tk.Y)
        for sid in sorted(INGREDIENTS):
            self._beh_list.insert(tk.END, f" S{sid:02d}   {INGREDIENTS[sid].get('label','')}")
        self._beh_list.bind("<<ListboxSelect>>",self._beh_on_select)

        # Right pane: detail editor — sectioned cards
        right=tk.Frame(body,bg=BG_PANEL); right.pack(side=tk.LEFT,fill=tk.BOTH,expand=True)

        # Right-pane header — selected station + per-station test buttons
        rhdr=tk.Frame(right,bg=BG_PANEL); rhdr.pack(fill=tk.X,pady=(0,6))
        self._beh_lbl_title=tk.Label(rhdr,text="—",font=self.fLG,bg=BG_PANEL,fg=C_BLUE,anchor=tk.W)
        self._beh_lbl_title.pack(side=tk.LEFT,padx=4)
        self._beh_lbl_meta=tk.Label(rhdr,text="",font=self.fSM,bg=BG_PANEL,fg=C_MUTED,anchor=tk.W)
        self._beh_lbl_meta.pack(side=tk.LEFT,padx=12)
        self._beh_btn_eject=tk.Button(rhdr,text="↩ TEST EJECT",font=self.fBT,
                                      bg=C_AMBER,fg="#000",relief=tk.FLAT,padx=10,pady=4,
                                      command=self._beh_test_eject)
        self._beh_btn_eject.pack(side=tk.RIGHT,padx=4)
        self._beh_btn_drop=tk.Button(rhdr,text="🔬 TEST DROP",font=self.fBT,
                                     bg=C_GREEN,fg="#000",relief=tk.FLAT,padx=10,pady=4,
                                     command=self._beh_test_drop)
        self._beh_btn_drop.pack(side=tk.RIGHT,padx=4)

        # Section helper
        def make_section(parent,title):
            s=tk.Frame(parent,bg=BG_CARD,bd=0,highlightthickness=0)
            s.pack(fill=tk.X,pady=4)
            tk.Label(s,text=title,font=self.fBG,bg=BG_CARD,fg=C_GREEN,anchor=tk.W
                     ).pack(fill=tk.X,padx=10,pady=(8,4))
            inner=tk.Frame(s,bg=BG_CARD); inner.pack(fill=tk.X,padx=10,pady=(0,8))
            return inner

        # Section 1: DISPENSE
        sec_disp=make_section(right,"DISPENSE")
        self._beh_widgets={}  # holds every editable widget for tech-gating
        def _entry(parent,col,row,var=None,width=8,fg=C_WHITE):
            e=tk.Entry(parent,font=self.fSM,bg=BG_DARK,fg=fg,
                       insertbackground=C_GREEN,relief=tk.FLAT,width=width,
                       justify=tk.RIGHT)
            e.grid(row=row,column=col,padx=4,pady=3,sticky=tk.W)
            self._beh_widgets.setdefault("entries",[]).append(e)
            return e
        def _label(parent,col,row,text,fg=C_MUTED):
            tk.Label(parent,text=text,font=self.fBG,bg=BG_CARD,fg=fg,anchor=tk.W
                     ).grid(row=row,column=col,padx=(8,4),pady=3,sticky=tk.W)
        # Dispense row 1: ingredient name + station label
        _label(sec_disp,0,0,"Station label")
        self._beh_e_label=tk.Entry(sec_disp,font=self.fSM,bg=BG_DARK,fg=C_WHITE,
                                    insertbackground=C_GREEN,relief=tk.FLAT,width=22)
        self._beh_e_label.grid(row=0,column=1,padx=4,pady=3,sticky=tk.W,columnspan=3)
        self._beh_widgets.setdefault("entries",[]).append(self._beh_e_label)
        _label(sec_disp,4,0,"Ingredient name")
        self._beh_e_ing=tk.Entry(sec_disp,font=self.fSM,bg=BG_DARK,fg=C_GREEN,
                                  insertbackground=C_GREEN,relief=tk.FLAT,width=22)
        self._beh_e_ing.grid(row=0,column=5,padx=4,pady=3,sticky=tk.W,columnspan=3)
        self._beh_widgets["entries"].append(self._beh_e_ing)
        # Dispense row 2: mode / threshold / accel / decel / ML
        _label(sec_disp,0,1,"Mode")
        self._beh_v_mode=tk.StringVar(value="auto")
        self._beh_cb_mode=ttk.Combobox(sec_disp,textvariable=self._beh_v_mode,
                                       values=("auto","micro","normal"),state="readonly",
                                       width=10,style="Beh.TCombobox",font=self.fSM)
        self._beh_cb_mode.grid(row=1,column=1,padx=4,pady=3,sticky=tk.W)
        self._beh_widgets.setdefault("combos",[]).append(self._beh_cb_mode)
        _label(sec_disp,2,1,"Threshold (g)")
        self._beh_e_thr=_entry(sec_disp,3,1,fg=C_AMBER)
        _label(sec_disp,4,1,"Accel step")
        self._beh_e_acc=_entry(sec_disp,5,1,fg=C_CYAN)
        _label(sec_disp,6,1,"Decel factor")
        self._beh_e_dec=_entry(sec_disp,7,1,fg=C_CYAN)
        _label(sec_disp,8,1,"ML")
        self._beh_v_ml=tk.BooleanVar(value=True)
        self._beh_cb_ml=tk.Checkbutton(sec_disp,variable=self._beh_v_ml,bg=BG_CARD,
                                       activebackground=BG_CARD,fg=C_GREEN,
                                       selectcolor=BG_DARK,highlightthickness=0,bd=0)
        self._beh_cb_ml.grid(row=1,column=9,padx=4,pady=3,sticky=tk.W)
        self._beh_widgets.setdefault("checks",[]).append(self._beh_cb_ml)
        self._beh_v_mode.trace_add("write",lambda *a: self._beh_mode_changed())
        # Dispense row 3: per-station scale tolerance + fall delay + target offset
        _label(sec_disp,0,2,"Tolerance (±g)")
        self._beh_e_tol=_entry(sec_disp,1,2,fg=C_AMBER)
        _label(sec_disp,2,2,"Fall delay (s)")
        self._beh_e_fall=_entry(sec_disp,3,2,fg=C_AMBER)
        # Target offset — "stop early" semantics.
        # + value : auger stops this many grams BEFORE target
        #           (e.g. offset 0.5, target 5 g → stops at 4.5 g)
        # 0       : land on target
        # − value : aim above target (rarely useful)
        # Clamped to ±5 g for safety.
        _label(sec_disp,4,2,"Stop early (g)")
        self._beh_e_off=_entry(sec_disp,5,2,fg=C_CYAN)

        # Section 1.5: PRECISION DISPENSING (UI FIX so the Behavior tab
        # actually affects the fill loop). The four fields below drive the
        # cutoff & top-up logic in Station._fill_t:
        #   • Fine margin (g) — fine fill stops this far below target
        #   • Inflight comp (g) — static in-flight estimate (settles into
        #     the pan after the auger stops)
        #   • Max top-up pulses — budget of micro-pulses for the top-up
        #     loop (raising it helps reach target on slow-flow ingredients)
        #   • Pulse-ms LRG/MED/SML/TINY — gap-adapted top-up pulse durations
        # Bumping margins down or pulse durations up makes the fill MORE
        # accurate (less undershoot) at the cost of slight overshoot risk.
        sec_prec=make_section(right,"PRECISION DISPENSING  (affects cutoff & top-up)")
        # Pre-stop + settle + final-micro stage (NEW operator-spec). These
        # drive the UNIVERSAL late-fill behavior:
        #   • Final-micro amount (g) : bulk dispense stops this many grams
        #     before the target weight. Default 1.0.
        #   • Settling delay (s)     : motor-off wait between bulk stop and
        #     the final-micro pulse phase. Default 1.0.
        _label(sec_prec,0,0,"Final-micro amount (g)"); self._beh_e_fmA =_entry(sec_prec,1,0,fg=C_AMBER)
        _label(sec_prec,2,0,"Settling delay (s)");     self._beh_e_sdS =_entry(sec_prec,3,0,fg=C_AMBER)
        _label(sec_prec,4,0,"Fine margin (g)");    self._beh_e_fineM =_entry(sec_prec,5,0,fg=C_AMBER)
        _label(sec_prec,6,0,"Inflight comp (g)");  self._beh_e_infl  =_entry(sec_prec,7,0,fg=C_AMBER)
        _label(sec_prec,0,1,"Max top-up pulses");  self._beh_e_tumx  =_entry(sec_prec,1,1,fg=C_CYAN)
        # (legacy field — present so existing tuned configs load; new
        # behavior is driven by Final-micro + Settling above.)
        _label(sec_prec,2,1,"Auto micro-tail (g)");self._beh_e_tail  =_entry(sec_prec,3,1,fg=C_AMBER)
        _label(sec_prec,0,2,"Pulse ms LRG");       self._beh_e_pL    =_entry(sec_prec,1,2)
        _label(sec_prec,2,2,"Pulse ms MED");       self._beh_e_pM    =_entry(sec_prec,3,2)
        _label(sec_prec,4,2,"Pulse ms SML");       self._beh_e_pS    =_entry(sec_prec,5,2)
        _label(sec_prec,6,2,"Pulse ms TINY");      self._beh_e_pT    =_entry(sec_prec,7,2)

        # Section 2: SERVO — DROP TO CONVEYOR
        sec_drop=make_section(right,"SERVO  ▸  DROP TO CONVEYOR")
        _label(sec_drop,0,0,"Start angle"); self._beh_e_dstart=_entry(sec_drop,1,0,fg=C_WHITE)
        _label(sec_drop,2,0,"End angle");   self._beh_e_dend  =_entry(sec_drop,3,0,fg=C_WHITE)
        _label(sec_drop,4,0,"Speed dt (s)");self._beh_e_dspd  =_entry(sec_drop,5,0,fg=C_AMBER)
        _label(sec_drop,6,0,"Hold (s)");    self._beh_e_dhold =_entry(sec_drop,7,0,fg=C_AMBER)
        _label(sec_drop,0,1,"Tap count");   self._beh_e_dcount=_entry(sec_drop,1,1)
        _label(sec_drop,2,1,"Tap low");     self._beh_e_dlow  =_entry(sec_drop,3,1)
        _label(sec_drop,4,1,"Tap high");    self._beh_e_dhigh =_entry(sec_drop,5,1)
        _label(sec_drop,6,1,"Tap dt (s)");  self._beh_e_dtdt  =_entry(sec_drop,7,1,fg=C_AMBER)

        # Section 3: SERVO — EJECT TO BASKET
        sec_ej=make_section(right,"SERVO  ▸  EJECT TO INSIDE BASKET")
        _label(sec_ej,0,0,"Start angle");   self._beh_e_estart=_entry(sec_ej,1,0,fg=C_WHITE)
        _label(sec_ej,2,0,"End angle");     self._beh_e_eend  =_entry(sec_ej,3,0,fg=C_WHITE)
        _label(sec_ej,4,0,"Speed dt (s)");  self._beh_e_espd  =_entry(sec_ej,5,0,fg=C_AMBER)
        _label(sec_ej,6,0,"Hold (s)");      self._beh_e_ehold =_entry(sec_ej,7,0,fg=C_AMBER)

        # Section 4: RETURN
        sec_rt=make_section(right,"SERVO  ▸  RETURN POSITION")
        _label(sec_rt,0,0,"Return angle");  self._beh_e_rang  =_entry(sec_rt,1,0,fg=C_WHITE)
        _label(sec_rt,2,0,"Speed dt (s)");  self._beh_e_rspd  =_entry(sec_rt,3,0,fg=C_AMBER)

        # Section 5: SERVO SAFETY (per-station envelope + per-movement timeout)
        # Safe angle limits clamp every commanded angle so the servo can't be
        # driven against its absolute mechanical stops; the move timeout caps
        # how long any single drop/eject sequence can run before being aborted
        # and the servo released.
        sec_safe=make_section(right,"SERVO  ▸  SAFETY")
        _label(sec_safe,0,0,"Safe min (°)");   self._beh_e_smin =_entry(sec_safe,1,0,fg=C_AMBER)
        _label(sec_safe,2,0,"Safe max (°)");   self._beh_e_smax =_entry(sec_safe,3,0,fg=C_AMBER)
        _label(sec_safe,4,0,"Move timeout (s)"); self._beh_e_smove=_entry(sec_safe,5,0,fg=C_CYAN)

        # Footer help
        tk.Label(right,
                 text=("Mode: auto = pulse if target ≤ threshold else continuous   "
                       "•  micro = always pulse   •  normal = always continuous   "
                       "•  ML preserves the trained voltage model when ON"),
                 font=self.fSM,bg=BG_PANEL,fg=C_MUTED,justify=tk.LEFT,anchor=tk.W,
                 wraplength=720).pack(fill=tk.X,padx=4,pady=(8,2))

        # Initial selection: first station
        self._beh_current_sid=None
        self._beh_list.select_set(0)
        self._beh_on_select(None)

    # ── Tech-gating: enable/disable every editable widget on the Behavior tab ──
    def _apply_tech_gates(self):
        if not hasattr(self,"_beh_widgets"): return
        on=self._tech
        state="normal" if on else "disabled"
        ro_state="readonly" if on else "disabled"
        for e in self._beh_widgets.get("entries",[]):
            try: e.configure(state=state)
            except Exception: pass
        for c in self._beh_widgets.get("combos",[]):
            try: c.configure(state=ro_state)
            except Exception: pass
        for c in self._beh_widgets.get("checks",[]):
            try: c.configure(state=state)
            except Exception: pass
        # Test buttons
        if hasattr(self,"_beh_btn_drop"):
            self._beh_btn_drop.configure(state=state)
            self._beh_btn_eject.configure(state=state)
        # Mode-specific re-gate (threshold disabled when mode!=auto)
        self._beh_mode_changed()
        # Diagnostics tab buttons too
        if hasattr(self,"_diag_widgets"):
            for w in self._diag_widgets:
                try: w.configure(state=state)
                except Exception: pass
        # Single-station Diagnostic tab
        if hasattr(self,"_diag_single_widgets"):
            for w in self._diag_single_widgets:
                try: w.configure(state=state)
                except Exception: pass
        if hasattr(self,"_diag_single_combos"):
            for c in self._diag_single_combos:
                try: c.configure(state=ro_state)
                except Exception: pass

    def _beh_mode_changed(self,*_):
        if not hasattr(self,"_beh_e_thr"): return
        if not self._tech:
            self._beh_e_thr.configure(state="disabled",fg=C_DIM); return
        if self._beh_v_mode.get()=="auto":
            self._beh_e_thr.configure(state="normal",fg=C_AMBER)
        else:
            self._beh_e_thr.configure(state="disabled",fg=C_DIM)

    def _beh_on_select(self,_event):
        sel=self._beh_list.curselection()
        if not sel:
            return
        idx=sel[0]
        sids=sorted(INGREDIENTS.keys())
        if idx>=len(sids): return
        sid=sids[idx]
        self._beh_current_sid=sid
        info=INGREDIENTS[sid]
        self._beh_lbl_title.config(text=f"S{sid:02d}  ▸  {info.get('label','')}")
        self._beh_lbl_meta.config(text=f"Board {info.get('board','—')}  /  Channel {info.get('ch','—')}")
        self._beh_populate(info)

    def _beh_populate(self,info):
        """Push fields from INGREDIENTS[sid] into the right-pane widgets."""
        def set_entry(widget,text):
            try:
                was=widget["state"]
                widget.configure(state="normal")
                widget.delete(0,tk.END); widget.insert(0,text)
                widget.configure(state=was)
            except Exception: pass
        set_entry(self._beh_e_label,info.get("label",""))
        set_entry(self._beh_e_ing,  info.get("ingredient_name",info.get("label","")))
        self._beh_v_mode.set(info.get("dispense_mode",DEFAULT_DISPENSE_MODE))
        set_entry(self._beh_e_thr, f"{float(info.get('micro_threshold_g',DEFAULT_MICRO_THRESHOLD_G)):.2f}")
        set_entry(self._beh_e_acc, f"{float(info.get('accel_step',ACCEL_STEP)):.3f}")
        set_entry(self._beh_e_dec, f"{float(info.get('decel_factor',DEFAULT_DECEL_FACTOR)):.2f}")
        self._beh_v_ml.set(bool(info.get("ml_model_enabled",DEFAULT_ML_ENABLED)))
        set_entry(self._beh_e_tol,  f"{float(info.get('scale_tolerance_grams',DEFAULT_SCALE_TOLERANCE_G)):.2f}")
        set_entry(self._beh_e_fall, f"{float(info.get('fall_delay_seconds',  DEFAULT_FALL_DELAY_S)):.2f}")
        set_entry(self._beh_e_off,  f"{float(info.get('target_offset_g',     DEFAULT_TARGET_OFFSET_G)):.2f}")
        s=_coerce_servo(info.get("servo"))
        set_entry(self._beh_e_dstart,str(s["drop_angle_start"]))
        set_entry(self._beh_e_dend,  str(s["drop_angle_end"]))
        set_entry(self._beh_e_dspd,  f"{s['drop_speed_dt']:.4f}")
        set_entry(self._beh_e_dhold, f"{s['drop_hold_s']:.2f}")
        set_entry(self._beh_e_dcount,str(s["drop_tap_count"]))
        set_entry(self._beh_e_dlow,  str(s["drop_tap_low"]))
        set_entry(self._beh_e_dhigh, str(s["drop_tap_high"]))
        set_entry(self._beh_e_dtdt,  f"{s['drop_tap_dt']:.3f}")
        set_entry(self._beh_e_estart,str(s["eject_angle_start"]))
        set_entry(self._beh_e_eend,  str(s["eject_angle_end"]))
        set_entry(self._beh_e_espd,  f"{s['eject_speed_dt']:.4f}")
        set_entry(self._beh_e_ehold, f"{s['eject_hold_s']:.2f}")
        set_entry(self._beh_e_rang,  str(s["return_angle"]))
        set_entry(self._beh_e_rspd,  f"{s['return_speed_dt']:.4f}")
        set_entry(self._beh_e_smin,  f"{float(info.get('safe_angle_min',DEFAULT_SAFE_ANGLE_MIN)):.0f}")
        set_entry(self._beh_e_smax,  f"{float(info.get('safe_angle_max',DEFAULT_SAFE_ANGLE_MAX)):.0f}")
        set_entry(self._beh_e_smove, f"{float(info.get('servo_move_timeout_s',SERVO_MOVE_TIMEOUT_S_DEFAULT)):.1f}")
        # Precision-dispensing fields (populate from live config; these
        # propagate directly into the fill loop on Save).
        set_entry(self._beh_e_fineM, f"{float(info.get('fine_margin_g',         DEFAULT_FINE_MARGIN_G)):.2f}")
        set_entry(self._beh_e_infl,  f"{float(info.get('inflight_compensation_g',DEFAULT_INFLIGHT_COMP_G)):.2f}")
        set_entry(self._beh_e_tumx,  f"{int(info.get('max_topup_pulses',         DEFAULT_MAX_TOPUP_PULSES))}")
        set_entry(self._beh_e_pL,    f"{int(info.get('pulse_ms_large',           DEFAULT_PULSE_MS_LARGE))}")
        set_entry(self._beh_e_pM,    f"{int(info.get('pulse_ms_medium',          DEFAULT_PULSE_MS_MEDIUM))}")
        set_entry(self._beh_e_pS,    f"{int(info.get('pulse_ms_small',           DEFAULT_PULSE_MS_SMALL))}")
        set_entry(self._beh_e_pT,    f"{int(info.get('pulse_ms_tiny',            DEFAULT_PULSE_MS_TINY))}")
        set_entry(self._beh_e_tail,  f"{float(info.get('auto_micro_tail_g',      DEFAULT_AUTO_MICRO_TAIL_G)):.2f}")
        # NEW operator-spec fields
        set_entry(self._beh_e_fmA,   f"{float(info.get('final_micro_amount_g',   DEFAULT_FINAL_MICRO_AMOUNT_G)):.2f}")
        set_entry(self._beh_e_sdS,   f"{float(info.get('settling_delay_s',       DEFAULT_SETTLING_DELAY_S)):.2f}")
        self._apply_tech_gates()

    def _beh_collect(self):
        """Read the right pane into a dict, validate, return (data, err)."""
        sid=self._beh_current_sid
        if sid is None: return None,"no station selected"
        try: thr=float(self._beh_e_thr.get())
        except Exception: return None,"threshold must be numeric"
        try: accel=float(self._beh_e_acc.get())
        except Exception: return None,"accel must be numeric"
        try: decel=float(self._beh_e_dec.get())
        except Exception: return None,"decel must be numeric"
        try: tol=float(self._beh_e_tol.get())
        except Exception: return None,"tolerance must be numeric"
        try: fall=float(self._beh_e_fall.get())
        except Exception: return None,"fall delay must be numeric"
        try: off=float(self._beh_e_off.get())
        except Exception: return None,"target offset must be numeric"
        if not (0<=thr<=1000): return None,"threshold out of range (0–1000 g)"
        if not (0.001<=accel<=5.0): return None,"accel out of range (0.001–5.0)"
        if not (0.1<=decel<=10.0): return None,"decel out of range (0.1–10.0)"
        if not (0.05<=tol<=50.0):   return None,"tolerance out of range (0.05–50 g)"
        if not (0.0<=fall<=30.0):   return None,"fall delay out of range (0–30 s)"
        if not (-TARGET_OFFSET_MAX_G<=off<=TARGET_OFFSET_MAX_G):
            return None,f"target offset out of range (±{TARGET_OFFSET_MAX_G:g} g)"
        try: smin=float(self._beh_e_smin.get())
        except Exception: return None,"safe min angle must be numeric"
        try: smax=float(self._beh_e_smax.get())
        except Exception: return None,"safe max angle must be numeric"
        try: smove=float(self._beh_e_smove.get())
        except Exception: return None,"move timeout must be numeric"
        if not (SAFE_ANGLE_MIN_LIMIT<=smin<=SAFE_ANGLE_MAX_LIMIT):
            return None,f"safe min out of hardware range ({SAFE_ANGLE_MIN_LIMIT:.0f}–{SAFE_ANGLE_MAX_LIMIT:.0f}°)"
        if not (SAFE_ANGLE_MIN_LIMIT<=smax<=SAFE_ANGLE_MAX_LIMIT):
            return None,f"safe max out of hardware range ({SAFE_ANGLE_MIN_LIMIT:.0f}–{SAFE_ANGLE_MAX_LIMIT:.0f}°)"
        if smax<=smin:
            return None,"safe max must be greater than safe min"
        if not (1.0<=smove<=60.0):
            return None,"move timeout out of range (1–60 s)"
        # Servo fields → run through _coerce_servo for clamping
        raw_servo={
            "drop_angle_start": self._beh_e_dstart.get(),
            "drop_angle_end":   self._beh_e_dend.get(),
            "drop_speed_dt":    self._beh_e_dspd.get(),
            "drop_hold_s":      self._beh_e_dhold.get(),
            "drop_tap_count":   self._beh_e_dcount.get(),
            "drop_tap_low":     self._beh_e_dlow.get(),
            "drop_tap_high":    self._beh_e_dhigh.get(),
            "drop_tap_dt":      self._beh_e_dtdt.get(),
            "eject_angle_start":self._beh_e_estart.get(),
            "eject_angle_end":  self._beh_e_eend.get(),
            "eject_speed_dt":   self._beh_e_espd.get(),
            "eject_hold_s":     self._beh_e_ehold.get(),
            "return_angle":     self._beh_e_rang.get(),
            "return_speed_dt":  self._beh_e_rspd.get(),
        }
        servo=_coerce_servo(raw_servo)
        # Precision-dispensing fields. Validated + clamped so the user can't
        # save values that would break the fill loop (e.g. negative pulses).
        try: fineM=float(self._beh_e_fineM.get())
        except Exception: return None,"fine margin must be numeric"
        try: infl =float(self._beh_e_infl.get())
        except Exception: return None,"inflight comp must be numeric"
        try: tumx =int(float(self._beh_e_tumx.get()))
        except Exception: return None,"max top-up pulses must be an integer"
        try: pL=int(float(self._beh_e_pL.get()))
        except Exception: return None,"pulse ms LRG must be an integer"
        try: pM=int(float(self._beh_e_pM.get()))
        except Exception: return None,"pulse ms MED must be an integer"
        try: pS=int(float(self._beh_e_pS.get()))
        except Exception: return None,"pulse ms SML must be an integer"
        try: pT=int(float(self._beh_e_pT.get()))
        except Exception: return None,"pulse ms TINY must be an integer"
        try: tail=float(self._beh_e_tail.get())
        except Exception: return None,"auto micro-tail must be numeric"
        try: final_micro_amount=float(self._beh_e_fmA.get())
        except Exception: return None,"final-micro amount must be numeric"
        try: settling_delay=float(self._beh_e_sdS.get())
        except Exception: return None,"settling delay must be numeric"
        if not (0.0<=final_micro_amount<=FINAL_MICRO_AMOUNT_MAX_G):
            return None,f"final-micro amount out of range (0–{FINAL_MICRO_AMOUNT_MAX_G:g} g)"
        if not (0.0<=settling_delay<=SETTLING_DELAY_MAX_S):
            return None,f"settling delay out of range (0–{SETTLING_DELAY_MAX_S:g} s)"
        if not (0.0<=fineM<=20.0):  return None,"fine margin out of range (0–20 g)"
        if not (0.0<=infl<=20.0):   return None,"inflight comp out of range (0–20 g)"
        if not (1<=tumx<=50):       return None,"max top-up pulses out of range (1–50)"
        if not (0.0<=tail<=AUTO_MICRO_TAIL_MAX_G):
            return None,f"auto micro-tail out of range (0–{AUTO_MICRO_TAIL_MAX_G:g} g)"
        for nm,v in (("LRG",pL),("MED",pM),("SML",pS),("TINY",pT)):
            if not (5<=v<=500):
                return None,f"pulse ms {nm} out of range (5–500)"
        return {
            "label":(self._beh_e_label.get().strip() or f"S{sid}"),
            "ingredient_name":(self._beh_e_ing.get().strip() or self._beh_e_label.get().strip()),
            "dispense_mode":self._beh_v_mode.get(),
            "micro_threshold_g":thr,
            "accel_step":accel,
            "decel_factor":decel,
            "ml_model_enabled":bool(self._beh_v_ml.get()),
            "servo":servo,
            "scale_tolerance_grams":tol,
            "fall_delay_seconds":fall,
            "target_offset_g":off,
            "safe_angle_min":smin,
            "safe_angle_max":smax,
            "servo_move_timeout_s":smove,
            # Precision dispensing — flow directly into Station.apply_behavior
            # via refresh_station_behavior() on save.
            "fine_margin_g":fineM,
            "inflight_compensation_g":infl,
            "max_topup_pulses":tumx,
            "pulse_ms_large":pL,
            "pulse_ms_medium":pM,
            "pulse_ms_small":pS,
            "pulse_ms_tiny":pT,
            "auto_micro_tail_g":tail,
            # NEW operator-spec — these drive the universal pre-stop + settle
            # + final-micro stage in Station._fill_t (every fill uses these).
            "final_micro_amount_g":final_micro_amount,
            "settling_delay_s":settling_delay,
        },None

    def _beh_save_current(self):
        if not self._tech:
            log_msg("Technician Mode required to save behavior","error"); return
        data,err=self._beh_collect()
        if err:
            log_msg(err,"error")
            self.show_toast(f"Save failed: {err}","error")
            return
        sid=self._beh_current_sid
        INGREDIENTS[sid].update(data)
        save_station_config(INGREDIENTS)
        # refresh_station_behavior() re-applies every saved field into the
        # live Station instance (Station.apply_behavior), so the very next
        # dispense uses these values — no restart needed.
        refresh_station_behavior()
        # Mirror label to other tabs
        if hasattr(self,"_ren") and sid in self._ren:
            self._ren[sid]["var"].set(data["label"])
        if hasattr(self,"_inv_rows"):
            self._inv_refresh()
        # Refresh list label
        sids=sorted(INGREDIENTS.keys())
        idx=sids.index(sid)
        self._beh_list.delete(idx)
        self._beh_list.insert(idx,f" S{sid:02d}   {data['label']}")
        self._beh_list.select_set(idx)
        self._beh_lbl_title.config(text=f"S{sid:02d}  ▸  {data['label']}")
        # Surfaced validation feedback so the operator/technician can see
        # the new values are actually live (was a complaint that Behavior
        # changes seemed to do nothing).
        st=stations.get(sid)
        eff=(f"tol±{getattr(st,'scale_tolerance_grams',0):.2f}g "
             f"fall {getattr(st,'fall_delay_seconds',0):.1f}s "
             f"offset {getattr(st,'target_offset_g',0):+.2f}g "
             f"final_micro {getattr(st,'final_micro_amount_g',0):.2f}g "
             f"settle {getattr(st,'settling_delay_s',0):.2f}s "
             f"fineM {getattr(st,'fine_margin_g',0):.2f}g "
             f"infl {getattr(st,'inflight_compensation_g',0):.2f}g "
             f"topup={getattr(st,'max_topup_pulses',0)}") if st else "applied"
        log_msg(f"S{sid:02d} behavior saved & APPLIED → {eff}","ok")
        self.show_toast(
            f"S{sid:02d} Behavior saved and APPLIED to live fill loop.\n{eff}",
            "ok")

    def _beh_reload(self):
        if self._beh_current_sid is None: return
        self._beh_populate(INGREDIENTS[self._beh_current_sid])
        log_msg("Behavior pane reloaded from config","info")

    def _beh_reset_defaults(self):
        if not self._tech:
            log_msg("Technician Mode required","error"); return
        if self._beh_current_sid is None: return
        # Build a defaulted info merged with current label/ingredient (don't blow those away)
        cur=INGREDIENTS[self._beh_current_sid]
        info={
            "label":cur.get("label",""),
            "ingredient_name":cur.get("ingredient_name",cur.get("label","")),
            "dispense_mode":DEFAULT_DISPENSE_MODE,
            "micro_threshold_g":DEFAULT_MICRO_THRESHOLD_G,
            "accel_step":ACCEL_STEP,
            "decel_factor":DEFAULT_DECEL_FACTOR,
            "ml_model_enabled":DEFAULT_ML_ENABLED,
            "servo":dict(DEFAULT_SERVO),
            "scale_tolerance_grams":DEFAULT_SCALE_TOLERANCE_G,
            "fall_delay_seconds":DEFAULT_FALL_DELAY_S,
            "target_offset_g":DEFAULT_TARGET_OFFSET_G,
            "safe_angle_min":DEFAULT_SAFE_ANGLE_MIN,
            "safe_angle_max":DEFAULT_SAFE_ANGLE_MAX,
            "servo_move_timeout_s":SERVO_MOVE_TIMEOUT_S_DEFAULT,
            "fine_margin_g":DEFAULT_FINE_MARGIN_G,
            "inflight_compensation_g":DEFAULT_INFLIGHT_COMP_G,
            "max_topup_pulses":DEFAULT_MAX_TOPUP_PULSES,
            "pulse_ms_large":DEFAULT_PULSE_MS_LARGE,
            "pulse_ms_medium":DEFAULT_PULSE_MS_MEDIUM,
            "pulse_ms_small":DEFAULT_PULSE_MS_SMALL,
            "pulse_ms_tiny":DEFAULT_PULSE_MS_TINY,
            "auto_micro_tail_g":DEFAULT_AUTO_MICRO_TAIL_G,
            "final_micro_amount_g":DEFAULT_FINAL_MICRO_AMOUNT_G,
            "settling_delay_s":DEFAULT_SETTLING_DELAY_S,
        }
        self._beh_populate(info)
        log_msg(f"S{self._beh_current_sid:02d} reset to defaults (not saved yet)","info")

    def _beh_test_drop(self):
        sid=self._beh_current_sid
        if sid is None: return
        if not self._tech:
            log_msg("Technician Mode required for tests","error"); return
        if orch.running:
            log_msg("Recipe in progress — manual test blocked","error"); return
        log_msg(f"S{sid:02d} TEST DROP","info")
        threading.Thread(target=stations[sid]._drop_t,daemon=True).start()

    def _beh_test_eject(self):
        sid=self._beh_current_sid
        if sid is None: return
        if not self._tech:
            log_msg("Technician Mode required for tests","error"); return
        if orch.running:
            log_msg("Recipe in progress — manual test blocked","error"); return
        log_msg(f"S{sid:02d} TEST EJECT","info")
        threading.Thread(target=stations[sid]._eject_blocking,daemon=True).start()

    # ══════════════════════════════════════════════════════════════════════════
    # TAB — DIAGNOSTIC (single tab; two cards: single-station + per-station grid)
    # All buttons require Technician Mode. All tests refuse while a recipe is
    # in flight. The MOTOR ON button auto-stops after MAX_MANUAL_MOTOR_S.
    # ══════════════════════════════════════════════════════════════════════════
    def _tab_diagnostic(self,p):
        # Page header
        hdr=tk.Frame(p,bg=BG_PANEL); hdr.pack(fill=tk.X,padx=10,pady=8)
        tk.Label(hdr,text="Diagnostic",font=self.fLG,bg=BG_PANEL,fg=C_WHITE
                 ).pack(side=tk.LEFT)
        tk.Label(hdr,text=f"single-station console + per-station manual tests — "
                          f"motor capped at {MANUAL_TEST_VOLTAGE:.1f}V × {MAX_MANUAL_MOTOR_S:.0f}s",
                 font=self.fSM,bg=BG_PANEL,fg=C_MUTED
                 ).pack(side=tk.LEFT,padx=(10,0),pady=(4,0))

        # Tab-level scrollable container
        wrap=tk.Frame(p,bg=BG_PANEL); wrap.pack(fill=tk.BOTH,expand=True,padx=0,pady=(0,4))
        cv_outer=tk.Canvas(wrap,bg=BG_PANEL,highlightthickness=0)
        sb_outer=ttk.Scrollbar(wrap,orient=tk.VERTICAL,command=cv_outer.yview)
        inner_outer=tk.Frame(cv_outer,bg=BG_PANEL)
        cv_outer.configure(yscrollcommand=sb_outer.set)
        cv_outer.pack(side=tk.LEFT,fill=tk.BOTH,expand=True)
        sb_outer.pack(side=tk.RIGHT,fill=tk.Y)
        win=cv_outer.create_window((0,0),window=inner_outer,anchor=tk.NW)
        def _on_inner_configure(e):
            cv_outer.configure(scrollregion=cv_outer.bbox("all"))
        def _on_canvas_resize(e):
            cv_outer.itemconfigure(win,width=e.width)
        inner_outer.bind("<Configure>",_on_inner_configure)
        cv_outer.bind("<Configure>",_on_canvas_resize)
        # Mouse-wheel scrolling — use bind_all while pointer is inside the tab,
        # so wheel events from any child widget reach the outer canvas.
        def _wheel(e):
            d=0
            if getattr(e,"num",None)==4: d=-1
            elif getattr(e,"num",None)==5: d=1
            elif getattr(e,"delta",0)>0: d=-1
            elif getattr(e,"delta",0)<0: d=1
            if d: cv_outer.yview_scroll(d,"units")
        def _wheel_bind(_e=None):
            cv_outer.bind_all("<MouseWheel>",_wheel)
            cv_outer.bind_all("<Button-4>",_wheel)
            cv_outer.bind_all("<Button-5>",_wheel)
        def _wheel_unbind(_e=None):
            cv_outer.unbind_all("<MouseWheel>")
            cv_outer.unbind_all("<Button-4>")
            cv_outer.unbind_all("<Button-5>")
        wrap.bind("<Enter>",_wheel_bind)
        wrap.bind("<Leave>",_wheel_unbind)

        # Section helper (visual card with title bar)
        def _section(parent,title):
            card=tk.Frame(parent,bg=BG_CARD,highlightbackground=C_BORDER,highlightthickness=1)
            card.pack(fill=tk.X,padx=10,pady=(8,4))
            bar=tk.Frame(card,bg=BG_CARD2); bar.pack(fill=tk.X)
            tk.Label(bar,text=title,font=self.fBG,bg=BG_CARD2,fg=C_GREEN,anchor=tk.W
                     ).pack(side=tk.LEFT,padx=10,pady=6)
            body=tk.Frame(card,bg=BG_CARD); body.pack(fill=tk.X,padx=4,pady=(2,6))
            return body

        # Section 1 — Station Diagnostic Dispense (single-station console)
        sec1=_section(inner_outer,"STATION DIAGNOSTIC DISPENSE")
        self._build_diag_single(sec1)
        # Section 2 — Manual Motor / Servo Test (per-station test grid)
        sec2=_section(inner_outer,"MANUAL MOTOR / SERVO TEST")
        self._build_diag_grid(sec2)

        self._apply_tech_gates()

    def _build_diag_grid(self,p):
        """Per-station manual test grid (TARE / DROP / EJECT / motor / dispense).
        Builds into the supplied parent frame; no own scrollbar (tab-level scroll
        handles overflow)."""
        # Column headers
        cols=[("S#",4),("Ingredient",20),("Tare",6),("Drop",6),("Eject",6),
              ("Motor ON",10),("Motor OFF",10),("Disp 5g",8),("Status",30)]
        th=tk.Frame(p,bg=BG_CARD2); th.pack(fill=tk.X,padx=8,pady=(2,1))
        for i,(h,w) in enumerate(cols):
            tk.Label(th,text=h,font=self.fBG,bg=BG_CARD2,fg=C_DIM,width=w,anchor=tk.W
                     ).grid(row=0,column=i,padx=4,pady=4,sticky=tk.W)

        # Rows packed directly (parent scroll handles overflow)
        inner=tk.Frame(p,bg=BG_CARD); inner.pack(fill=tk.X,padx=8,pady=(0,2))

        self._diag_rows={}
        self._diag_widgets=[]   # tracked for tech-gating
        for i,sid in enumerate(sorted(INGREDIENTS.keys())):
            info=INGREDIENTS[sid]
            bg2=BG_CARD if i%2==0 else BG_CARD2
            row=tk.Frame(inner,bg=bg2); row.pack(fill=tk.X,pady=1)
            tk.Label(row,text=f"S{sid:02d}",font=self.fBG,bg=bg2,fg=C_BLUE,
                     width=4,anchor=tk.W
                     ).grid(row=0,column=0,padx=4,pady=6,sticky=tk.W)
            lbl_ing=tk.Label(row,text=info.get("label","")[:22],font=self.fLB,
                             bg=bg2,fg=C_WHITE,width=20,anchor=tk.W)
            lbl_ing.grid(row=0,column=1,padx=4,sticky=tk.W)
            def mkbtn(parent,text,col,bg,fg,cmd):
                b=tk.Button(parent,text=text,font=self.fBG,bg=bg,fg=fg,relief=tk.FLAT,
                            padx=6,pady=4,command=cmd)
                b.grid(row=0,column=col,padx=2,sticky=tk.W)
                self._diag_widgets.append(b)
                return b
            mkbtn(row,"TARE",   2, BG_CARD2, C_WHITE, lambda s=sid: self._diag_run(s,"tare"))
            mkbtn(row,"DROP",   3, C_GREEN, "#000",   lambda s=sid: self._diag_run(s,"drop"))
            mkbtn(row,"EJECT",  4, C_AMBER, "#000",   lambda s=sid: self._diag_run(s,"eject"))
            mkbtn(row,"ON",     5, C_BLUE, "#fff",    lambda s=sid: self._diag_run(s,"motor_on"))
            mkbtn(row,"OFF",    6, C_RED,  "#fff",    lambda s=sid: self._diag_run(s,"motor_off"))
            mkbtn(row,"DISP 5g",7, C_CYAN, "#000",    lambda s=sid: self._diag_run(s,"disp"))
            v_status=tk.StringVar(value="—")
            lbl_status=tk.Label(row,textvariable=v_status,font=self.fSM,bg=bg2,
                                fg=C_MUTED,width=30,anchor=tk.W)
            lbl_status.grid(row=0,column=8,padx=4,sticky=tk.W)
            self._diag_rows[sid]={"ing_label":lbl_ing,"status":v_status,"status_lbl":lbl_status}

        # Footer
        tip=tk.Frame(p,bg=BG_CARD); tip.pack(fill=tk.X,padx=8,pady=(2,4))
        tk.Label(tip,
                 text=("All tests require Technician Mode and refuse while a recipe is running. "
                       "MOTOR ON pulses the auger at 2.5V for up to 3s with an automatic stop. "
                       "DROP / EJECT use the per-station servo sequence configured on the Behavior tab."),
                 font=self.fSM,bg=BG_CARD,fg=C_MUTED,wraplength=900,justify=tk.LEFT,anchor=tk.W
                 ).pack(fill=tk.X,padx=4,pady=4)

    def _diag_set_status(self,sid,level,msg):
        """Update the status cell for a station — color-coded by level."""
        d=self._diag_rows.get(sid)
        if not d: return
        col={"OK":C_GREEN,"BUSY":C_AMBER,"OFFLINE":C_DIM,
             "TIMEOUT":C_AMBER,"ERROR":C_RED}.get(level,C_MUTED)
        ts=datetime.now().strftime("%H:%M:%S")
        d["status"].set(f"{level}  {ts}  {msg}"[:60])
        d["status_lbl"].config(fg=col)
        # Refresh ingredient label in case it changed via Station Mgr
        d["ing_label"].config(text=INGREDIENTS.get(sid,{}).get("label","")[:22])

    def _diag_run(self,sid,kind):
        """Dispatch a manual test on a background thread so the UI stays responsive."""
        if not self._tech:
            log_msg("Technician Mode required for diagnostics","error"); return
        if orch.running:
            self._diag_set_status(sid,"BUSY","recipe running"); return
        st=stations.get(sid)
        if st is None: return
        # Refuse offline boards up front
        if st.board not in boards or not boards[st.board].connected:
            self._diag_set_status(sid,"OFFLINE",f"board {st.board}"); return

        def _worker():
            try:
                if kind=="tare":
                    st.tare()
                    self.after(0, lambda: self._diag_set_status(sid,"OK","tare sent"))
                elif kind=="drop":
                    st._drop_t()
                    self.after(0, lambda: self._diag_set_status(sid,"OK","drop sequence done"))
                elif kind=="eject":
                    st._eject_blocking()
                    self.after(0, lambda: self._diag_set_status(sid,"OK","eject sequence done"))
                elif kind=="motor_on":
                    level,msg=st.test_motor_pulse(MANUAL_TEST_VOLTAGE,MAX_MANUAL_MOTOR_S)
                    self.after(0, lambda lv=level,m=msg: self._diag_set_status(sid,lv,m))
                elif kind=="motor_off":
                    st._stop()
                    self.after(0, lambda: self._diag_set_status(sid,"OK","motor stopped"))
                elif kind=="disp":
                    level,msg=st.test_dispense_blocking(MANUAL_DISP_TEST_TARGET_G,30.0)
                    self.after(0, lambda lv=level,m=msg: self._diag_set_status(sid,lv,m))
            except Exception as e:
                self.after(0, lambda em=str(e): self._diag_set_status(sid,"ERROR",em))
        threading.Thread(target=_worker,daemon=True).start()

    # ══════════════════════════════════════════════════════════════════════════
    # Single-station diagnostic console (rendered inside Section 1 of _tab_diagnostic)
    # ══════════════════════════════════════════════════════════════════════════
    def _build_diag_single(self,p):
        """Single-station diagnostic console. Builds into the supplied parent
        frame — no page-level header (the section card title provides that)."""
        self._diag_single_widgets=[]
        self._diag_single_combos=[]

        # Subtitle / tip line
        tip=tk.Frame(p,bg=BG_CARD); tip.pack(fill=tk.X,padx=4,pady=(2,4))
        tk.Label(tip,text="select one station to inspect live state and run TARE→FILL / STOP / DROP / EJECT",
                 font=self.fSM,bg=BG_CARD,fg=C_MUTED,anchor=tk.W
                 ).pack(side=tk.LEFT,padx=8)

        # Selector row
        srow=tk.Frame(p,bg=BG_CARD); srow.pack(fill=tk.X,padx=8,pady=(0,6))
        tk.Label(srow,text="Station:",font=self.fBG,bg=BG_CARD,fg=C_DIM
                 ).pack(side=tk.LEFT,padx=(0,6))
        self._diag_single_sids=sorted(INGREDIENTS.keys())
        opts=[f"S{sid:02d} — {INGREDIENTS[sid].get('label','—')}" for sid in self._diag_single_sids]
        self._diag_single_cb=ttk.Combobox(srow,values=opts,state="readonly",
                                          font=self.fSM,width=32)
        self._diag_single_cb.pack(side=tk.LEFT)
        self._diag_single_cb.bind("<<ComboboxSelected>>",self._diag_single_on_select)
        self._diag_single_combos.append(self._diag_single_cb)

        # Detail card
        card=tk.Frame(p,bg=BG_CARD2,highlightbackground=C_BORDER,highlightthickness=1)
        card.pack(fill=tk.X,padx=8,pady=4)
        for c in range(4): card.columnconfigure(c,weight=1)
        self._diag_single_lbl_title=tk.Label(card,text="— select a station —",
                                             font=self.fLG,bg=BG_CARD2,fg=C_BLUE,anchor=tk.W)
        self._diag_single_lbl_title.grid(row=0,column=0,columnspan=4,padx=10,pady=(8,0),sticky=tk.W)
        self._diag_single_lbl_meta=tk.Label(card,text="Board — / Channel —",
                                            font=self.fSM,bg=BG_CARD2,fg=C_MUTED,anchor=tk.W)
        self._diag_single_lbl_meta.grid(row=1,column=0,columnspan=4,padx=10,pady=(0,6),sticky=tk.W)

        def _kv(row,col,key,val_widget):
            tk.Label(card,text=key,font=self.fSM,bg=BG_CARD2,fg=C_MUTED,anchor=tk.W
                     ).grid(row=row,column=col*2,padx=(10,4),pady=2,sticky=tk.W)
            val_widget.grid(row=row,column=col*2+1,padx=(0,10),pady=2,sticky=tk.W)

        self._diag_single_lbl_status=tk.Label(card,text="—",font=self.fBG,bg=BG_CARD2,fg=C_MUTED,anchor=tk.W)
        self._diag_single_lbl_tgt   =tk.Label(card,text="0.00 g",font=self.fBG,bg=BG_CARD2,fg=C_AMBER,anchor=tk.W)
        self._diag_single_lbl_motor =tk.Label(card,text="OFF",font=self.fBG,bg=BG_CARD2,fg=C_MUTED,anchor=tk.W)
        self._diag_single_lbl_servo =tk.Label(card,text="—",font=self.fBG,bg=BG_CARD2,fg=C_DIM,anchor=tk.W)
        self._diag_single_lbl_tol   =tk.Label(card,text="±3.00 g",font=self.fBG,bg=BG_CARD2,fg=C_CYAN,anchor=tk.W)
        self._diag_single_lbl_fall  =tk.Label(card,text="2.00 s",font=self.fBG,bg=BG_CARD2,fg=C_CYAN,anchor=tk.W)
        _kv(2,0,"Status:",self._diag_single_lbl_status)
        _kv(2,1,"Target:",self._diag_single_lbl_tgt)
        _kv(3,0,"Motor:", self._diag_single_lbl_motor)
        _kv(3,1,"Servo:", self._diag_single_lbl_servo)
        _kv(4,0,"Tol.:",  self._diag_single_lbl_tol)
        _kv(4,1,"Fall:",  self._diag_single_lbl_fall)

        # Big actual-weight readout
        self._diag_single_lbl_actual=tk.Label(card,text="0.00",font=self.fXL,
                                              bg=BG_CARD2,fg=C_GREEN,anchor=tk.CENTER)
        self._diag_single_lbl_actual.grid(row=5,column=0,columnspan=4,
                                          padx=10,pady=(8,4),sticky="ew")
        tk.Label(card,text="actual weight (g)",font=self.fSM,bg=BG_CARD2,fg=C_DIM,anchor=tk.CENTER
                 ).grid(row=6,column=0,columnspan=4,padx=10,pady=(0,8),sticky="ew")

        # Dispense input row
        irow=tk.Frame(p,bg=BG_CARD); irow.pack(fill=tk.X,padx=8,pady=(8,2))
        tk.Label(irow,text="Dispense (g):",font=self.fLB,bg=BG_CARD,fg=C_MUTED
                 ).pack(side=tk.LEFT,padx=(0,6))
        self._diag_single_e_amt=tk.Entry(irow,width=6,justify=tk.CENTER,
                                         bg=BG_CARD2,fg=C_GREEN,insertbackground=C_GREEN,
                                         relief=tk.FLAT,font=self.fSM)
        self._diag_single_e_amt.insert(0,"5.0")
        self._diag_single_e_amt.pack(side=tk.LEFT)
        self._diag_single_widgets.append(self._diag_single_e_amt)

        # Button row
        brow=tk.Frame(p,bg=BG_CARD); brow.pack(fill=tk.X,padx=8,pady=(2,8))
        def mkbtn(text,bg,fg,cmd,col):
            b=tk.Button(brow,text=text,font=self.fBT,bg=bg,fg=fg,relief=tk.FLAT,
                        padx=10,pady=4,width=10,command=cmd)
            b.grid(row=0,column=col,padx=4,pady=2,sticky=tk.W)
            self._diag_single_widgets.append(b)
            return b
        self._diag_single_btn_start=mkbtn("▶ START",C_GREEN,"#000",
                                          lambda: self._diag_single_run("start"),0)
        self._diag_single_btn_stop =mkbtn("⬛ STOP", C_RED,  "#fff",
                                          lambda: self._diag_single_run("stop"), 1)
        self._diag_single_btn_drop =mkbtn("⬇ DROP", C_AMBER,"#000",
                                          lambda: self._diag_single_run("drop"), 2)
        self._diag_single_btn_eject=mkbtn("↺ EJECT","#9c27b0","#fff",
                                          lambda: self._diag_single_run("eject"),3)

        # Footer
        ftr=tk.Frame(p,bg=BG_CARD); ftr.pack(fill=tk.X,padx=8,pady=(0,8))
        self._diag_single_lbl_msg=tk.Label(ftr,text="",font=self.fSM,bg=BG_CARD,
                                           fg=C_MUTED,wraplength=900,justify=tk.LEFT,anchor=tk.W)
        self._diag_single_lbl_msg.pack(fill=tk.X,padx=4,pady=(2,0))
        tk.Label(ftr,
                 text=("Requires Technician Mode. Refuses while a recipe is running or board offline. "
                       "START performs TARE → 0.3 s settle → FILL on a worker thread; STOP issues a soft stop. "
                       "DROP/EJECT use the per-station servo sequence configured on the Behavior tab."),
                 font=self.fSM,bg=BG_CARD,fg=C_MUTED,wraplength=900,justify=tk.LEFT,anchor=tk.W
                 ).pack(fill=tk.X,padx=4,pady=(4,0))

    def _diag_single_on_select(self,event=None):
        idx=self._diag_single_cb.current()
        if idx<0: return
        sid=self._diag_single_sids[idx]
        self._diag_single_sid=sid
        info=INGREDIENTS.get(sid,{})
        self._diag_single_lbl_title.config(text=f"S{sid:02d}  ▸  {info.get('label','—')}")
        self._diag_single_lbl_meta.config(text=f"Board {info.get('board','—')}  /  Channel {info.get('ch','—')}")
        self._diag_single_refresh_static(sid)
        self._diag_single_msg(f"S{sid:02d} selected","info")

    def _diag_single_refresh_static(self,sid):
        st=stations.get(sid)
        if st is None: return
        w=st.ui_w if st.running else st.smooth()
        if w<ZERO_RANGE: w=0.0
        self._diag_single_lbl_actual.config(text=f"{w:.2f}")
        self._diag_single_lbl_status.config(text=st.status,
                                            fg=STATUS_COLORS.get(st.status,C_MUTED))
        self._diag_single_lbl_tgt.config(text=f"{st.target:.2f} g")
        if st.running:
            self._diag_single_lbl_motor.config(text=f"RUNNING {st.ui_v:.2f} V",fg=C_GREEN)
        else:
            self._diag_single_lbl_motor.config(text="OFF",fg=C_MUTED)
        servo_states={"POURING","TAPPING","RESETTING","EJECTING","EJECTED",
                      "SERVO_DROP_TO_CONVEYOR","SERVO_EJECT_TO_BASKET"}
        if st.status in servo_states:
            self._diag_single_lbl_servo.config(text=st.status,
                                               fg=STATUS_COLORS.get(st.status,C_AMBER))
        else:
            self._diag_single_lbl_servo.config(text="—",fg=C_DIM)
        if hasattr(self,"_diag_single_lbl_tol"):
            tol=getattr(st,"scale_tolerance_grams",DEFAULT_SCALE_TOLERANCE_G)
            self._diag_single_lbl_tol.config(text=f"±{tol:.2f} g")
        if hasattr(self,"_diag_single_lbl_fall"):
            fall=getattr(st,"fall_delay_seconds",DEFAULT_FALL_DELAY_S)
            self._diag_single_lbl_fall.config(text=f"{fall:.2f} s")

    def _diag_single_msg(self,text,level="info"):
        col={"ok":C_GREEN,"warn":C_AMBER,"error":C_RED,"info":C_BLUE}.get(level,C_MUTED)
        ts=datetime.now().strftime("%H:%M:%S")
        if hasattr(self,"_diag_single_lbl_msg"):
            self._diag_single_lbl_msg.config(text=f"[{ts}] {text}",fg=col)

    def _diag_single_reset_state(self,sid):
        """Force-reset a station to a safe, ready-to-test state.
        Used BEFORE every new diagnostic run (clears stale state from prior
        runs — fixes the 'second run shows error' symptom) and AFTER
        STOP/DROP/EJECT (so the same station can be tested repeatedly).
        Does NOT interrupt a running fill — callers check `st.running` first.
        Resets: motor pwm, hour-tracker, running flag, target/actual/voltage,
        smoothing buffer + EMA (so the next tare starts from a clean filter
        state), and UI button group."""
        st=stations.get(sid)
        if st is None: return
        try: st._stop()                       # belt-and-braces: motor pwm = 0
        except Exception: pass
        try: motor_off(sid)                   # cleanup hour-tracker if still open
        except Exception: pass
        st.running=False
        st.target=0.0
        st.actual=0.0
        st.ui_v=0.0
        st.ui_w=0.0
        # Reset the smoothing filter so the next run isn't biased by stale data
        try:
            st.mbuf.clear(); st.ema=0.0; st.last_w=0.0
        except Exception: pass
        # Always force IDLE — even from ERROR/STOPPED — so a second click works
        # without requiring the operator to manually clear the error first.
        st.status="IDLE"
        # Reset Dashboard station-card buttons to idle group too
        try: st._btns("idle")
        except Exception: pass

    def _diag_single_watch_completion(self,sid,amt):
        """Background watcher: waits for the fill thread to complete, then
        posts a 'Ready for next diagnostic test' message and (on error)
        resets state. Hard-bounded by an overall timeout so a stuck fill
        never leaves the diagnostic UI in a 'waiting forever' state."""
        st=stations.get(sid)
        if st is None: return
        # Wait for the fill thread to actually start (start_fill spawns _fill_t).
        # Bound this short — if it never starts, the fill simply didn't happen.
        t0=time.monotonic()
        while not st.running and time.monotonic()-t0<3.0:
            time.sleep(0.05)
        # Wait for completion, hard-capped at 5 minutes (any fill should finish
        # well before this; the no-data/no-flow timeouts will trip first).
        deadline=time.monotonic()+300.0
        while st.running and time.monotonic()<deadline:
            time.sleep(0.1)
        # Capture outcome BEFORE any reset
        actual=getattr(st,"actual",0.0)
        status=getattr(st,"status","IDLE")
        # On error / stopped / fault → reset so next click works without manual fix
        if status in ("ERROR","STOPPED","FAULT") or st.running:
            self.after(0,lambda: self._diag_single_reset_state(sid))
        # Always post a clear "ready" message (regardless of outcome) so the
        # user knows the diagnostic round has finished and the station is
        # available again.
        self.after(0,lambda a=actual,s=status: self._diag_single_msg(
            f"S{sid:02d} → {a:.2f}g [{s}] · Ready for next diagnostic test","ok"))

    def _diag_single_run(self,kind):
        if not self._tech:
            return self._diag_single_msg("Technician Mode required","error")
        sid=getattr(self,"_diag_single_sid",None)
        if sid is None:
            return self._diag_single_msg("Select a station first","warn")
        if orch.running:
            return self._diag_single_msg("Refused — recipe in progress","warn")
        st=stations.get(sid)
        if st is None: return
        if st.board not in boards or not boards[st.board].connected:
            return self._diag_single_msg(f"Board {st.board} offline","error")
        amt=None
        if kind=="start":
            if st.running:
                return self._diag_single_msg(f"S{sid:02d} already running","warn")
            try:
                amt=float(self._diag_single_e_amt.get())
                if amt<=0: raise ValueError
            except Exception:
                return self._diag_single_msg("Invalid dispense amount","error")
            # Belt-and-braces: clear any stale state from a prior run before
            # we tare + fill. Fixes the "second run shows error" symptom by
            # guaranteeing motor/status/target/running flags start clean.
            self._diag_single_reset_state(sid)
        def _worker():
            try:
                if kind=="start":
                    st.tare(); time.sleep(0.3)
                    st.start_fill(amt)
                    self.after(0,lambda: self._diag_single_msg(
                        f"S{sid:02d} fill {amt:.1f}g started","ok"))
                    # Watch for completion in a separate daemon so this worker
                    # can return promptly. The watcher posts the "ready" msg.
                    threading.Thread(
                        target=self._diag_single_watch_completion,
                        args=(sid,amt),daemon=True).start()
                elif kind=="stop":
                    st.stop()
                    self.after(0,lambda: self._diag_single_reset_state(sid))
                    self.after(0,lambda: self._diag_single_msg(
                        f"S{sid:02d} STOP sent · Ready for next diagnostic test","ok"))
                elif kind=="drop":
                    st._drop_t()
                    self.after(0,lambda: self._diag_single_reset_state(sid))
                    self.after(0,lambda: self._diag_single_msg(
                        f"S{sid:02d} drop done · Ready for next diagnostic test","ok"))
                elif kind=="eject":
                    st._eject_blocking()
                    self.after(0,lambda: self._diag_single_reset_state(sid))
                    self.after(0,lambda: self._diag_single_msg(
                        f"S{sid:02d} eject done · Ready for next diagnostic test","ok"))
            except Exception as e:
                self.after(0,lambda em=str(e): self._diag_single_msg(em,"error"))
                # On any worker exception, reset state so the next attempt isn't blocked
                self.after(0,lambda: self._diag_single_reset_state(sid))
        threading.Thread(target=_worker,daemon=True).start()

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 2 — PRODUCTION LOG
    # ══════════════════════════════════════════════════════════════════════════
    def _tab_log(self,p):
        # UI FIX: Production Log was reported invisible. The Treeview itself
        # was rendered, but the surrounding header is now larger, includes a
        # row-count badge so the operator always sees "live" status, and the
        # treeview is forced to expand into the remaining space. Larger row
        # height + readable column widths so production data is genuinely
        # readable on the touchscreen.
        hdr=tk.Frame(p,bg=BG_PANEL); hdr.pack(fill=tk.X,padx=10,pady=8)
        tk.Label(hdr,text="Production Log",font=self.fLG,bg=BG_PANEL,fg=C_WHITE
                 ).pack(side=tk.LEFT)
        self._log_count_var=tk.StringVar(value="— rows")
        tk.Label(hdr,textvariable=self._log_count_var,font=self.fSM,
                 bg=BG_PANEL,fg=C_CYAN).pack(side=tk.LEFT,padx=12)
        tk.Label(hdr,text=f"file: {LOG_FILE}",font=self.fSM,
                 bg=BG_PANEL,fg=C_MUTED).pack(side=tk.LEFT,padx=12)
        tk.Button(hdr,text="⟳ Refresh",font=self.fBT,bg=BG_CARD2,fg=C_WHITE,
                  relief=tk.FLAT,padx=8,pady=3,
                  command=self._refresh_log).pack(side=tk.RIGHT,padx=5)

        # Canonical 10-column schema (matches log_production output)
        cols=["OrderID","Timestamp","Recipe","St","Ingredient","Target","Actual","Δ","Tol","Status"]
        cws =[90,180,240,45,180,80,80,75,75,80]
        tf=tk.Frame(p,bg=BG_DARK); tf.pack(fill=tk.BOTH,expand=True,padx=10,pady=(0,8))
        vsb=ttk.Scrollbar(tf,orient=tk.VERTICAL)
        hsb=ttk.Scrollbar(tf,orient=tk.HORIZONTAL)
        self.ltree=ttk.Treeview(tf,columns=cols,show="headings",
                                yscrollcommand=vsb.set,xscrollcommand=hsb.set,
                                height=20)
        vsb.config(command=self.ltree.yview)
        hsb.config(command=self.ltree.xview)
        sty=ttk.Style()
        sty.configure("Treeview",background=BG_CARD,foreground=C_WHITE,
                      fieldbackground=BG_CARD,rowheight=30,font=(FONT_MONO,11))
        sty.configure("Treeview.Heading",background=BG_CARD2,foreground=C_GREEN,
                      font=(FONT_SANS,11,"bold"))
        sty.map("Treeview",background=[("selected",BG_PANEL)])
        for c,w in zip(cols,cws):
            self.ltree.heading(c,text=c); self.ltree.column(c,width=w,minwidth=w,anchor=tk.CENTER)
        self.ltree.tag_configure("warn",foreground=C_AMBER)
        self.ltree.tag_configure("ok",  foreground=C_WHITE)
        self.ltree.tag_configure("old",  foreground=C_MUTED)
        self.ltree.grid(row=0,column=0,sticky="nsew")
        vsb.grid(row=0,column=1,sticky="ns"); hsb.grid(row=1,column=0,sticky="ew")
        tf.rowconfigure(0,weight=1); tf.columnconfigure(0,weight=1)
        self._refresh_log()

    _LOG_NCOLS=10  # canonical column count

    def _refresh_log(self):
        """Re-read production_log.csv and display latest 200 rows.

        Handles two historical CSV formats:
          - New (10-col): OrderID,Timestamp,Recipe,Station,Tea,Target_g,Actual_g,Delta_g,Tol_g,Status
          - Old (5-col):  Timestamp,OrderID,IngredientName,TargetWeight,ActualWeight
        Rows with wrong column counts are normalised and still shown (greyed).
        """
        for row in self.ltree.get_children(): self.ltree.delete(row)
        if not os.path.exists(LOG_FILE):
            if hasattr(self,"_log_count_var"):
                self._log_count_var.set("0 rows — file not yet created")
            return
        try:
            with open(LOG_FILE,newline="") as f:
                reader=csv.reader(f)
                all_rows=[r for r in reader if any(c.strip() for c in r)]  # skip blank
        except Exception as e:
            log_msg(f"Log read error: {e}","error"); return
        # Determine header row(s) to skip
        data_rows=[]
        for r in all_rows:
            if r and r[0].strip().lower() in ("orderid","timestamp"): continue  # skip any header
            data_rows.append(r)
        # Show newest first, up to 200 rows
        N=self._LOG_NCOLS
        for row in reversed(data_rows[-200:]):
            nc=len(row)
            if nc==N:
                tag="warn" if row[9].strip().upper()=="WARN" else "ok"
                self.ltree.insert("","end",values=row,tags=(tag,))
            elif nc==5:
                # Old format: Timestamp,OrderID,IngredientName,TargetWeight,ActualWeight
                # Map to: OrderID,Timestamp,Recipe,St,Ingredient,Target,Actual,Δ,Tol,Status
                ts,oid,ing,tgt,act=row[0],row[1],row[2],row[3],row[4]
                try:
                    delta=float(act)-float(tgt)
                    d_s=f"{delta:+.2f}"
                except Exception:
                    d_s="—"
                normalised=[oid,ts,"—","—",ing,tgt,act,d_s,"—","LEGACY"]
                self.ltree.insert("","end",values=normalised,tags=("old",))
            else:
                # Partial row — pad/truncate and show greyed
                padded=(row+[""]*N)[:N]
                self.ltree.insert("","end",values=padded,tags=("old",))
        # Update visible row-count badge so the operator can see the log is live
        if hasattr(self,"_log_count_var"):
            shown=len(self.ltree.get_children())
            self._log_count_var.set(f"{shown} rows (of {len(data_rows)} total)")

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 3 — HEALTH & MAINTENANCE
    # ══════════════════════════════════════════════════════════════════════════
    def _tab_health(self,p):
        tk.Label(p,text="Board Connectivity Matrix",font=self.fLG,
                 bg=BG_DARK,fg=C_WHITE).pack(anchor=tk.W,padx=15,pady=(12,4))
        matrix=tk.Frame(p,bg=BG_CARD,highlightbackground=C_BORDER,highlightthickness=1)
        matrix.pack(fill=tk.X,padx=15,pady=(0,12))
        for c,h in enumerate(["Board","Port","Status","Latency","Err-Retries"]):
            tk.Label(matrix,text=h,font=self.fBG,bg=BG_CARD2,fg=C_DIM,
                     width=14,anchor=tk.W).grid(row=0,column=c,padx=4,pady=4,sticky=tk.W)
        self._hlth={}
        for i,bid in enumerate([1,2,3,4],1):
            bg2=BG_CARD if i%2==0 else "#111416"
            vals=[tk.StringVar(value="—") for _ in range(5)]
            self._hlth[bid]=vals
            defaults=["—","—","—","—ms","—"]
            for c,v in enumerate(vals):
                v.set(defaults[c])
                tk.Label(matrix,textvariable=v,font=self.fMD,bg=bg2,
                         fg=C_WHITE,width=14,anchor=tk.W
                         ).grid(row=i,column=c,padx=4,pady=3,sticky=tk.W)

        tk.Label(p,text="Predictive Maintenance — Motor Run Hours",font=self.fLG,
                 bg=BG_DARK,fg=C_WHITE).pack(anchor=tk.W,padx=15,pady=(6,4))
        mf=tk.Frame(p,bg=BG_CARD,highlightbackground=C_BORDER,highlightthickness=1)
        mf.pack(fill=tk.X,padx=15,pady=(0,12))
        for c,h in enumerate(["St","Ingredient","Run Hours","Alert"]):
            tk.Label(mf,text=h,font=self.fBG,bg=BG_CARD2,fg=C_DIM,
                     width=14,anchor=tk.W).grid(row=0,column=c,padx=4,pady=4,sticky=tk.W)
        self._maint={}
        for i,sid in enumerate(stations,1):
            bg2=BG_CARD if i%2==0 else "#111416"
            hv=tk.StringVar(value="0.000"); av=tk.StringVar(value="OK")
            self._maint[sid]={"h":hv,"a":av}
            tk.Label(mf,text=f"S{sid:02d}",font=self.fBG,bg=bg2,fg=C_BLUE,
                     width=14,anchor=tk.W).grid(row=i,column=0,padx=4,pady=2,sticky=tk.W)
            tk.Label(mf,text=stations[sid].label[:18],font=self.fLB,bg=bg2,fg=C_WHITE,
                     width=14,anchor=tk.W).grid(row=i,column=1,padx=4,sticky=tk.W)
            tk.Label(mf,textvariable=hv,font=self.fMD,bg=bg2,fg=C_CYAN,
                     width=14,anchor=tk.W).grid(row=i,column=2,padx=4,sticky=tk.W)
            tk.Label(mf,textvariable=av,font=self.fBG,bg=bg2,
                     width=14,anchor=tk.W).grid(row=i,column=3,padx=4,sticky=tk.W)

    # ── System log strip ─────────────────────────────────────────────────────
    def _log_strip(self):
        f=tk.Frame(self,bg=BG_PANEL,height=95); f.pack(side=tk.BOTTOM,fill=tk.X,padx=5,pady=(0,5))
        f.pack_propagate(False)
        tk.Label(f,text="SYSTEM LOG",font=self.fBG,bg=BG_PANEL,fg=C_DIM
                 ).pack(anchor=tk.W,padx=8,pady=(3,0))
        self.syslog=tk.Text(f,font=self.fMD,bg=BG_PANEL,fg=C_MUTED,
                            state=tk.DISABLED,relief=tk.FLAT,padx=8,
                            height=4,highlightthickness=0)
        self.syslog.pack(fill=tk.BOTH,expand=True,padx=3,pady=(0,3))
        for lv,co in [("ok",C_GREEN),("error",C_RED),("warn",C_AMBER),("info",C_BLUE),("ts",C_DIM)]:
            self.syslog.tag_config(lv,foreground=co)
        _log_callbacks.append(lambda e: self.after(0,lambda:self._ins(e)))
        # Also pop a toast for error / warn messages
        def _toast_cb(e):
            _,msg,lv=e
            if lv in ("error","warn"):
                self.after(0,lambda m=msg,l=lv:self.show_toast(m,l))
        _log_callbacks.append(_toast_cb)

    def _ins(self,entry):
        ts,msg,lv=entry
        self.syslog.config(state=tk.NORMAL)
        self.syslog.insert(tk.END,f"{ts}  ","ts"); self.syslog.insert(tk.END,msg+"\n",lv)
        lines=int(self.syslog.index(tk.END).split(".")[0])
        if lines>52: self.syslog.delete("1.0",f"{lines-50}.0")
        self.syslog.see(tk.END); self.syslog.config(state=tk.DISABLED)

    # ── Recipe logic ─────────────────────────────────────────────────────────
    def _sel_recipe(self,event=None):
        s=self.rlb.curselection()
        if not s: return
        name=self.rlb.get(s[0]); self._sel=name
        recipe=RECIPES[name]; self._ws=dict(recipe)
        self._active_sids=set(recipe.keys())
        self.lbl_tot.config(text=f"Total: {sum(recipe.values()):.0f} g")
        self._ing(list(recipe.items())); self._push_targets()
        self._highlight_cards()
        log_msg(f"Recipe: {name}","info")

    def _push_targets(self):
        for sid in range(1,14):
            st=stations[sid]
            if st.e_tgt:
                tgt=self._ws.get(sid,0.0)
                st.e_tgt.delete(0,tk.END); st.e_tgt.insert(0,f"{tgt:.1f}")

    def _highlight_cards(self):
        """Active cards glow green border; inactive cards dim. Disconnected glow red/grey."""
        for sid,card in self._cards.items():
            b = boards.get(stations[sid].board)
            if not b or not b.connected:
                card.config(highlightbackground=C_RED,highlightthickness=2,bg="#211516")
                stations[sid].e_tgt.config(state=tk.DISABLED)
                continue
            stations[sid].e_tgt.config(state=tk.NORMAL)
            if sid in self._active_sids:
                card.config(highlightbackground=C_ACT_BORDER,highlightthickness=2,bg=BG_CARD)
            else:
                card.config(highlightbackground=C_BORDER,highlightthickness=1,bg=C_DIM_BG)

    def _scale(self,new_t):
        if not self._sel: log_msg("Select recipe first","warn"); return
        orig=RECIPES[self._sel]; ot=sum(orig.values())
        if ot==0: return
        f=new_t/ot
        self._ws={sid:round(tgt*f,1) for sid,tgt in orig.items()}
        self.lbl_tot.config(text=f"Total: {new_t:.0f} g")
        self._ing(list(self._ws.items())); self._push_targets()
        log_msg(f"Scaled to {new_t:.0f}g","info")

    def _custom(self):
        try: v=float(self.e_cust.get()); assert v>0
        except Exception: log_msg("Invalid custom weight","error"); return
        if v > 150.0:
            messagebox.showerror("Weight Limit Exceeded", f"Cannot process {v}g. Maximum allowed is 150.0g.")
            return
        self._scale(v)

    # ── Actions ──────────────────────────────────────────────────────────────
    def _refresh_boards(self):
        for b in boards.values():
            if not b.connected: b.reconnect()
        log_msg("Sent refresh signals to disconnected boards","info")

    def _render_queue(self):
        """Rebuild all queue cards from self._queue list."""
        for w in self.q_inner.winfo_children(): w.destroy()
        if not self._queue:
            tk.Label(self.q_inner,text="Queue empty",font=self.fBG,
                     bg=BG_CARD2,fg=C_DIM).pack(padx=6,pady=6)
            return
        # Each card uses a 5-column grid layout:
        #   col 0: order ID (fixed width)
        #   col 1: recipe name (FLEXIBLE — absorbs slack via columnconfigure
        #          weight=1, wraps long names via wraplength so the card grows
        #          vertically rather than pushing buttons off-screen)
        #   col 2: total grams (fixed width, right-aligned)
        #   col 3: edit button (fixed width=2)
        #   col 4: delete button (fixed width=2)
        # Buttons keep fixed width and stay visible regardless of name length.
        for idx,item in enumerate(self._queue):
            bg=BG_CARD if idx%2==0 else BG_CARD2
            card=tk.Frame(self.q_inner,bg=bg,highlightbackground=C_BORDER,highlightthickness=1)
            card.pack(fill=tk.X,padx=3,pady=2)
            card.columnconfigure(1,weight=1)              # name column absorbs slack
            tk.Label(card,text=f"#{item['oid']}",font=self.fBG,bg=bg,fg=C_CYAN,
                     width=7,anchor=tk.W
                     ).grid(row=0,column=0,padx=4,pady=3,sticky=tk.W)
            # Full recipe name with wrapping — long names flow onto a second
            # line inside the recipe column, the card grows taller, buttons
            # stay anchored to the right edge.
            tk.Label(card,text=item['recipe'],font=self.fBG,bg=bg,fg=C_WHITE,
                     anchor=tk.W,justify=tk.LEFT,wraplength=180
                     ).grid(row=0,column=1,padx=2,pady=3,sticky="ew")
            tw=sum(item['weights'].values())
            tk.Label(card,text=f"{tw:.0f}g",font=self.fBG,bg=bg,fg=C_GREEN,
                     width=6,anchor=tk.E
                     ).grid(row=0,column=2,padx=4,sticky=tk.E)
            tk.Button(card,text="✎",font=self.fBG,bg=C_AMBER,fg="#000",relief=tk.FLAT,
                      width=2,padx=3,pady=1,
                      command=lambda i=idx:self._edit_queue_item(i)
                      ).grid(row=0,column=3,padx=1,pady=2)
            tk.Button(card,text="✕",font=self.fBG,bg=C_RED,fg="#fff",relief=tk.FLAT,
                      width=2,padx=3,pady=1,
                      command=lambda i=idx:self._delete_queue_item(i)
                      ).grid(row=0,column=4,padx=2,pady=2)

    def _delete_queue_item(self,idx):
        if 0<=idx<len(self._queue):
            removed=self._queue.pop(idx)
            log_msg(f"Removed #{removed['oid']} from queue","info")
            self._render_queue()

    def _edit_queue_item(self,idx):
        if not (0<=idx<len(self._queue)): return
        item=self._queue[idx]
        dlg=tk.Toplevel(self); dlg.title(f"Edit Order #{item['oid']}")
        dlg.configure(bg=BG_DARK); dlg.resizable(False,False)
        dlg.grab_set()
        tk.Label(dlg,text="Recipe:",font=self.fLB,bg=BG_DARK,fg=C_MUTED).grid(row=0,column=0,padx=10,pady=8,sticky=tk.W)
        rv=tk.StringVar(value=item['recipe'])
        rlb2=ttk.Combobox(dlg,textvariable=rv,values=list(RECIPES.keys()),state="readonly",width=30)
        rlb2.grid(row=0,column=1,padx=10,pady=8)
        tk.Label(dlg,text="Scale to (g):",font=self.fLB,bg=BG_DARK,fg=C_MUTED).grid(row=1,column=0,padx=10,sticky=tk.W)
        we=tk.Entry(dlg,font=self.fSM,bg=BG_CARD,fg=C_GREEN,insertbackground=C_GREEN,
                    relief=tk.FLAT,width=10)
        we.insert(0,f"{sum(item['weights'].values()):.0f}")
        we.grid(row=1,column=1,padx=10,pady=4,sticky=tk.W)
        def _apply():
            rname=rv.get()
            if rname not in RECIPES: return
            orig=RECIPES[rname]; ot=sum(orig.values())
            try: new_t=float(we.get()); assert new_t>0
            except Exception: log_msg("Invalid weight","error"); return
            f=new_t/ot if ot>0 else 1
            self._queue[idx]={"oid":item["oid"],"recipe":rname,
                               "weights":{s:round(w*f,1) for s,w in orig.items()}}
            log_msg(f"Updated #{item['oid']} → {rname} {new_t:.0f}g","ok")
            self._render_queue(); dlg.destroy()
        tk.Button(dlg,text="Update",font=self.fBT,bg=C_BLUE,fg="#fff",relief=tk.FLAT,
                  padx=10,pady=4,command=_apply).grid(row=2,column=0,columnspan=2,pady=10)

    def _add_queue(self):
        if not self._sel or not self._ws:
            log_msg("Select recipe first","warn"); return
        if len(self._queue)>=5:
            self.show_toast("Queue full — maximum 5 orders allowed","warn"); return
        req_bids=set([stations[sid].board for sid in self._ws.keys()])
        for bid in req_bids:
            if bid not in boards or not boards[bid].connected:
                self.show_toast(f"Cannot queue '{self._sel}': Board {bid} is disconnected","error"); return
        oid=next_oid()
        self._queue.append({"oid":oid,"recipe":self._sel,"weights":dict(self._ws)})
        self._render_queue()
        log_msg(f"Queued #{oid}  {self._sel} [{sum(self._ws.values()):.0f}g]","ok")

    def _start_queue(self):
        if orch.running: log_msg("System is already running","warn"); return
        if not self._queue: log_msg("Queue is empty","warn"); return
        # Peek (do not pop yet) — on_done() pops only on SUCCESS so a failed
        # order stays at the queue head for operator retry.
        item=self._queue[0]
        self.lbl_oid.config(text=f"ORDER  #{item['oid']}")
        ok=orch.dispatch(item["recipe"],item["weights"])
        if not ok:
            # Stock-check failed before the run thread started; on_done won't fire.
            self.show_toast("Order blocked: insufficient stock. Check Inventory tab.","error")

    def _auto_next(self):
        """Auto-advance to the next queued order, but only if idle and queue non-empty."""
        if orch.running: return
        if not self._queue: return
        self._start_queue()

    def _estop(self):
        orch.abort(); estop_all()
        self.set_cv("STOPPED"); self.set_mx("STOPPED")
        self._queue.clear(); self._render_queue()
        log_msg("GLOBAL E-STOP TRIGGERED. Queue purged.","error")

    def _dashboard_refresh(self):
        """Refresh dashboard UI state non-destructively.
        - If anything is actively running (recipe orchestrator OR any station
          fill), prompt the operator to confirm — refresh on its own does not
          stop hardware, but the operator should know the visible state may
          change as the loop reconciles.
        - Re-renders the order queue, refreshes inventory rows if visible,
          forces every station card label to redraw on the next tick, and
          posts a confirmation log entry.
        - Never deletes recipes, queue entries, station config, or inventory.
        - Never sends motor / servo commands."""
        active=orch.running or any(getattr(st,"running",False) for st in stations.values())
        if active:
            ok=messagebox.askyesno(
                "Refresh Dashboard",
                "Active dispensing or recipe is in progress.\n\n"
                "Refresh will only re-render the UI — it will NOT stop motors, "
                "abort the recipe, or modify saved data. Continue?")
            if not ok:
                log_msg("Refresh cancelled by operator","info"); return
        # Re-render the queue (rebuilds cards from self._queue list)
        self._render_queue()
        # Refresh inventory rows if the inventory tab has been built
        if hasattr(self,"_inv_rows"):
            try: self._inv_refresh()
            except Exception: pass
        # Force station-card redraw by clearing the dead-zone caches so the
        # next _loop tick repaints every weight/status label even if values
        # haven't changed enough to clear DEAD_ZONE.
        for st in stations.values():
            try: st.last_w=-9999.0
            except Exception: pass
        # Refresh the diagnostic single-station detail pane if a station is selected
        sid=getattr(self,"_diag_single_sid",None)
        if sid is not None:
            try: self._diag_single_refresh_static(sid)
            except Exception: pass
        log_msg("Dashboard refreshed","ok")

    def _tare_all(self):
        for b in boards.values(): b.write(b"T:ALL\n")
        for st in stations.values(): st.mbuf.clear(); st.ema=0.0
        log_msg("Tare all sent","info")

    def set_cv(self,s):
        col=STATUS_COLORS.get(s,C_MUTED)
        self.lbl_cv.config(text=f"CV:{s}",fg=col)
        self.lbl_cv2.config(text=s,fg=col)

    def set_mx(self,s):
        col=STATUS_COLORS.get(s,C_MUTED)
        self.lbl_mx.config(text=f"MX:{s}",fg=col)
        self.lbl_mx2.config(text=s,fg=col)

    def on_done(self):
        self.set_cv("IDLE")
        # Auto-refresh production log so the newly-written row is visible
        # without the operator having to switch tabs and click ⟳.
        try:
            if hasattr(self,"ltree"): self._refresh_log()
        except Exception:
            pass
        outcome=getattr(orch,"last_outcome","IDLE")
        ejected=getattr(orch,"last_ejected_lanes",[])
        def _labels(lst):
            try: return [f"S{s} ({stations[s].label})" for s in lst]
            except Exception: return [f"S{s}" for s in lst]

        if outcome=="SUCCESS":
            self.set_mx("COMPLETE")
            self.lbl_state.config(text="COMPLETE",fg=C_GREEN)
            # Pop the completed order; auto-advance to the next if any.
            if self._queue:
                self._queue.pop(0); self._render_queue()
            if self._queue:
                self.after(1000,self._auto_next)
        elif outcome=="PARTIAL":
            # Some lanes were ejected to the inside basket; recipe is "done"
            # but the operator should know what didn't make the cup. Pop the
            # order but DO NOT auto-advance — let the operator decide.
            self.set_mx("COMPLETE")
            self.lbl_state.config(text="PARTIAL",fg=C_AMBER)
            if self._queue:
                self._queue.pop(0); self._render_queue()
            who=", ".join(_labels(ejected)) if ejected else "—"
            self.show_toast(
                f"Order PARTIAL: {len(ejected)} ingredient(s) ejected ({who}). "
                f"Cup may be incomplete. Press START for the next order when ready.",
                "warn")
        elif outcome=="FAILED":
            self.set_mx("STOPPED")
            self.lbl_state.config(text="FAILED",fg=C_RED)
            failed=getattr(orch,"last_failed_lanes",[]) or ejected
            who=", ".join(_labels(failed)) if failed else "unknown station(s)"
            self.show_toast(
                f"Order FAILED on {who}. Belt and mixer stopped safely. "
                f"Clear the funnels, then press START to retry — or DELETE to skip.","error")
        else:
            self.set_mx("IDLE")
            self.lbl_state.config(text="IDLE",fg=C_MUTED)

    # ── GUI loop ─────────────────────────────────────────────────────────────
    def _loop(self):
        self.lbl_clk.config(text=datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        # Board dots + latency
        for bid,dot in self.bdots.items():
            b=boards.get(bid)
            on=b and b.connected
            dot.config(fg=C_GREEN if on else C_RED)
            lat=f"{b.latency_ms:.0f}ms" if on else "—ms"
            self.blat[bid].config(text=lat,fg=C_CYAN if on else C_DIM)
            # Health tab update
            if bid in self._hlth and b:
                hv=self._hlth[bid]
                hv[0].set(f"Board {bid}"); hv[1].set(BOARD_PORTS[bid])
                hv[2].set("ONLINE" if on else "OFFLINE")
                hv[3].set(lat)
                hv[4].set(str(sum(b._err.values())))

        # Orchestrator state
        state=orch.state
        self.lbl_state.config(text=state,fg=STATUS_COLORS.get(state,C_MUTED))

        # Station cards — weight, status, progress, disconnected, low-stock
        low=set(low_stock_stations())
        for st in stations.values():
            if st.id in self._stock_vars:
                stk = inventory.get(st.id, {}).get("stock", 0.0)
                self._stock_vars[st.id].set(f"Stock: {stk:.1f}g")
            
            w=st.ui_w if st.running else st.smooth()
            if w<ZERO_RANGE: w=0.0
            if abs(w-st.last_w)>DEAD_ZONE:
                st.last_w=w
                if st.lbl_w:
                    col=C_GREEN
                    if st.target>0 and abs(w-st.target)<0.3 and w>0.2: col="#ffffff"
                    st.lbl_w.config(text=f"{w:.2f}",fg=col)
            if st.lbl_s:
                b = boards.get(st.board)
                if not b or not b.connected:
                    st.lbl_s.config(text="DISCONNECTED", fg=C_RED)
                else:
                    st.lbl_s.config(text=st.status,fg=STATUS_COLORS.get(st.status,C_MUTED))
            if st.prog is not None:
                st.prog.set(min(100,(w/st.target*100)) if st.target>0 else 0.0)
            # Low-stock amber border (only when not already active/disconnected)
            if st.card_frame:
                b2=boards.get(st.board)
                if not b2 or not b2.connected:
                    pass  # already coloured red by _highlight_cards
                elif st.id in self._active_sids and st.id in low:
                    st.card_frame.config(highlightbackground=C_AMBER,highlightthickness=2)
                elif st.id in low and st.id not in self._active_sids:
                    st.card_frame.config(highlightbackground=C_AMBER,highlightthickness=2)

        # Maintenance tab
        for sid,d in self._maint.items():
            h=motor_hours(sid); alert=maint_alert(sid)
            d["h"].set(f"{h:.3f} h")
            d["a"].config(text="⚠ CHECK" if alert else "OK",
                          fg=C_RED if alert else C_GREEN) if hasattr(d["a"],"config") else None

        # Inventory tab — refresh live stock every ~2 s (50 × 40 ms ticks)
        self._inv_tick+=1
        if self._inv_tick>=50:
            self._inv_tick=0
            if hasattr(self,"_inv_rows"):
                self._inv_refresh()

        # Single-station Diagnostic tab — refresh selected station's live readout
        sid=getattr(self,"_diag_single_sid",None)
        if sid is not None and hasattr(self,"_diag_single_lbl_actual"):
            self._diag_single_refresh_static(sid)

        self.after(GUI_MS,self._loop)

    # ── Toast Notifications ───────────────────────────────────────────────────
    def show_toast(self,msg,level="info"):
        """Slide-in toast from top-right; auto-dismisses after 4 s."""
        col={"ok":C_GREEN,"error":C_RED,"warn":C_AMBER,"info":C_BLUE}.get(level,C_BLUE)
        t=tk.Frame(self,bg=col,highlightbackground=col,highlightthickness=1)
        t.place(relx=1.0,rely=0.0,anchor=tk.NE,x=-8,y=54)
        tk.Label(t,text=msg,font=self.fSM,bg=col,
                 fg="#000" if level in ("ok","warn") else "#fff",
                 padx=12,pady=8,wraplength=340,justify=tk.LEFT).pack()
        self.after(4000,lambda: t.destroy() if t.winfo_exists() else None)

    # ── Out-of-tolerance decision modal ───────────────────────────────────────
    def show_drop_decision(self,payload):
        """Modal popup invoked by Orchestrator when a fill misses ±DROP_TOLERANCE_G.
        Operator chooses: EJECT TO BASKET, RETRY DISPENSE, or CANCEL RECIPE.
        Closing the window is treated as CANCEL."""
        # If a previous modal is somehow still open, raise it
        existing=getattr(self,"_drop_modal",None)
        if existing and existing.winfo_exists():
            try: existing.lift(); existing.focus_force()
            except Exception: pass
            return
        sid=payload.get("sid"); label=payload.get("label","")
        target=float(payload.get("target",0.0))
        actual=float(payload.get("actual",0.0))
        delta=actual-target
        tol=float(payload.get("tolerance_g",DROP_TOLERANCE_G))
        over=delta>0
        verb="OVER" if over else "UNDER"
        sign="+" if delta>=0 else ""

        dlg=tk.Toplevel(self)
        self._drop_modal=dlg
        dlg.title("Weight out of tolerance")
        dlg.configure(bg=BG_PANEL)
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False,False)
        # Center on parent
        self.update_idletasks()
        w,h=560,360
        x=self.winfo_rootx()+(self.winfo_width()-w)//2
        y=self.winfo_rooty()+(self.winfo_height()-h)//2
        dlg.geometry(f"{w}x{h}+{max(0,x)}+{max(0,y)}")

        # Header
        hdr=tk.Frame(dlg,bg=C_RED if not over else C_AMBER,height=64)
        hdr.pack(fill=tk.X); hdr.pack_propagate(False)
        tk.Label(hdr,text="⚠  WEIGHT OUT OF TOLERANCE",
                 font=self.fT,bg=C_RED if not over else C_AMBER,fg="#000"
                 ).pack(side=tk.LEFT,padx=20,pady=18)

        # Body
        body=tk.Frame(dlg,bg=BG_PANEL)
        body.pack(fill=tk.BOTH,expand=True,padx=24,pady=(18,10))

        def row(parent,key,val,vfg=C_WHITE,kfg=C_MUTED):
            r=tk.Frame(parent,bg=BG_PANEL); r.pack(fill=tk.X,pady=3)
            tk.Label(r,text=key,font=self.fBT,bg=BG_PANEL,fg=kfg,width=14,anchor=tk.W
                     ).pack(side=tk.LEFT)
            tk.Label(r,text=val,font=self.fLG,bg=BG_PANEL,fg=vfg,anchor=tk.W
                     ).pack(side=tk.LEFT,padx=4)

        row(body,"Station",f"S{sid:02d}  {label}",vfg=C_BLUE)
        row(body,"Target",f"{target:.2f} g")
        row(body,"Actual",f"{actual:.2f} g")
        row(body,"Delta",f"{sign}{delta:.2f} g  ({verb} by {abs(delta):.2f} g)",
            vfg=C_RED if abs(delta)>tol else C_GREEN)
        row(body,"Tolerance",f"±{tol:.1f} g")

        tk.Label(body,text=("If you choose RETRY, clear the funnel manually first — "
                            "the previous batch is still in it."),
                 font=self.fSM,bg=BG_PANEL,fg=C_AMBER,wraplength=480,justify=tk.LEFT,
                 anchor=tk.W).pack(fill=tk.X,pady=(10,4))

        # Buttons
        btns=tk.Frame(dlg,bg=BG_PANEL); btns.pack(fill=tk.X,padx=24,pady=(6,18))

        def submit(choice):
            try: dlg.grab_release()
            except Exception: pass
            self._drop_modal=None
            try: dlg.destroy()
            except Exception: pass
            try: orch.submit_decision(choice)
            except Exception as e: log_msg(f"submit_decision error: {e}","error")

        b_eject=tk.Button(btns,text="↩  EJECT TO BASKET",font=self.fBT,
                          bg=C_AMBER,fg="#000",relief=tk.FLAT,padx=14,pady=10,
                          activebackground="#d68f00",
                          command=lambda: submit("EJECT"))
        b_eject.pack(side=tk.LEFT,fill=tk.X,expand=True,padx=(0,6))

        b_retry=tk.Button(btns,text="↻  RETRY DISPENSE",font=self.fBT,
                          bg=C_BLUE,fg="#fff",relief=tk.FLAT,padx=14,pady=10,
                          activebackground="#1f6cd6",
                          command=lambda: submit("RETRY"))
        b_retry.pack(side=tk.LEFT,fill=tk.X,expand=True,padx=6)

        b_cancel=tk.Button(btns,text="✕  CANCEL RECIPE",font=self.fBT,
                           bg=C_RED,fg="#fff",relief=tk.FLAT,padx=14,pady=10,
                           activebackground="#cc0020",
                           command=lambda: submit("CANCEL"))
        b_cancel.pack(side=tk.LEFT,fill=tk.X,expand=True,padx=(6,0))

        # Closing the window = CANCEL
        dlg.protocol("WM_DELETE_WINDOW",lambda: submit("CANCEL"))
        # Keyboard shortcuts: E / R / C
        dlg.bind("<KeyPress-e>",lambda e: submit("EJECT"))
        dlg.bind("<KeyPress-E>",lambda e: submit("EJECT"))
        dlg.bind("<KeyPress-r>",lambda e: submit("RETRY"))
        dlg.bind("<KeyPress-R>",lambda e: submit("RETRY"))
        dlg.bind("<KeyPress-c>",lambda e: submit("CANCEL"))
        dlg.bind("<KeyPress-C>",lambda e: submit("CANCEL"))
        dlg.bind("<Escape>",lambda e: submit("CANCEL"))
        dlg.focus_set()
        b_retry.focus_set()

    # ══════════════════════════════════════════════════════════════════════════
    # TAB — INVENTORY
    # ══════════════════════════════════════════════════════════════════════════
    def _tab_inventory(self,p):
        hdr=tk.Frame(p,bg=BG_PANEL); hdr.pack(fill=tk.X,padx=10,pady=8)
        tk.Label(hdr,text="Inventory & Container Management",font=self.fLG,
                 bg=BG_PANEL,fg=C_WHITE).pack(side=tk.LEFT)
        tk.Button(hdr,text="⟳ Refresh",font=self.fBT,bg=BG_CARD2,fg=C_WHITE,
                  relief=tk.FLAT,padx=8,pady=3,
                  command=self._inv_refresh).pack(side=tk.RIGHT,padx=5)

        # Column headers
        th=tk.Frame(p,bg=BG_CARD2); th.pack(fill=tk.X,padx=10,pady=(0,1))
        for i,(h,w2) in enumerate([("St",4),("Ingredient",22),("Stock (g)",10),
                                   ("Capacity (g)",12),("Status",8),("Refill (g)",10),(" ",6)]):
            tk.Label(th,text=h,font=self.fBG,bg=BG_CARD2,fg=C_DIM,width=w2,anchor=tk.W
                     ).grid(row=0,column=i,padx=4,pady=3,sticky=tk.W)

        sf=tk.Frame(p,bg=BG_DARK); sf.pack(fill=tk.BOTH,expand=True,padx=10,pady=(0,8))
        cv2=tk.Canvas(sf,bg=BG_DARK,highlightthickness=0)
        sb2=ttk.Scrollbar(sf,orient=tk.VERTICAL,command=cv2.yview)
        inner2=tk.Frame(cv2,bg=BG_DARK)
        inner2.bind("<Configure>",lambda e:cv2.configure(scrollregion=cv2.bbox("all")))
        cv2.create_window((0,0),window=inner2,anchor=tk.NW)
        cv2.configure(yscrollcommand=sb2.set)
        cv2.pack(side=tk.LEFT,fill=tk.BOTH,expand=True); sb2.pack(side=tk.RIGHT,fill=tk.Y)

        self._inv_rows={}   # sid -> {sv, stv, sl, nv, re}
        for i,sid in enumerate(sorted(INGREDIENTS.keys())):
            bg2=BG_CARD if i%2==0 else BG_CARD2
            row_f=tk.Frame(inner2,bg=bg2); row_f.pack(fill=tk.X,pady=1)
            tk.Label(row_f,text=f"S{sid:02d}",font=self.fBG,bg=bg2,fg=C_BLUE,
                     width=4,anchor=tk.W).grid(row=0,column=0,padx=4,pady=6,sticky=tk.W)
            nv=tk.StringVar(value=INGREDIENTS[sid]["label"][:24])
            tk.Label(row_f,textvariable=nv,font=self.fLB,bg=bg2,
                     fg=C_WHITE,width=24,anchor=tk.W).grid(row=0,column=1,padx=4,sticky=tk.W)
            sv=tk.StringVar(value="—"); stv=tk.StringVar(value="OK")
            tk.Label(row_f,textvariable=sv,font=self.fLB,bg=bg2,fg=C_GREEN,
                     width=10,anchor=tk.W).grid(row=0,column=2,padx=4,sticky=tk.W)
            cap=inventory.get(sid,{}).get("capacity",1000.0)
            tk.Label(row_f,text=f"{cap:.0f}",font=self.fLB,bg=bg2,fg=C_MUTED,
                     width=12,anchor=tk.W).grid(row=0,column=3,padx=4,sticky=tk.W)
            sl=tk.Label(row_f,textvariable=stv,font=self.fBG,bg=bg2,
                        width=10,anchor=tk.W)
            sl.grid(row=0,column=4,padx=4,sticky=tk.W)
            re=tk.Entry(row_f,font=self.fSM,bg=BG_DARK,fg=C_GREEN,
                        insertbackground=C_GREEN,relief=tk.FLAT,width=10)
            re.grid(row=0,column=5,padx=4,pady=3,sticky=tk.W)
            tk.Button(row_f,text="REFILL",font=self.fBG,bg="#1565c0",fg="#fff",
                      relief=tk.FLAT,padx=5,pady=2,
                      command=lambda s=sid,e=re:self._do_refill(s,e)
                      ).grid(row=0,column=6,padx=6)
            self._inv_rows[sid]={"sv":sv,"stv":stv,"sl":sl,"nv":nv,"re":re}
        self._inv_refresh()

    def _inv_refresh(self):
        low=set(low_stock_stations())
        for sid,d in self._inv_rows.items():
            stk=inventory.get(sid,{}).get("stock",0.0)
            d["sv"].set(f"{stk:.1f}")
            # Update ingredient name in case it was renamed
            if "nv" in d:
                d["nv"].set(INGREDIENTS.get(sid,{}).get("label","")[:24])
            if sid in low:
                d["stv"].set("⚠ LOW")
                d["sl"].config(fg=C_AMBER)
            else:
                d["stv"].set("✓ OK")
                d["sl"].config(fg=C_GREEN)

    def _do_refill(self,sid,entry):
        try: w=float(entry.get()); assert w>0
        except Exception: log_msg(f"S{sid}: invalid refill weight","error"); return
        refill_station(sid,w)
        entry.delete(0,tk.END)
        self._inv_refresh()

    # ══════════════════════════════════════════════════════════════════════════
    # TAB — RECIPES
    # ══════════════════════════════════════════════════════════════════════════
    def _tab_recipes(self,p):
        hdr=tk.Frame(p,bg=BG_PANEL); hdr.pack(fill=tk.X,padx=10,pady=8)
        tk.Label(hdr,text="Recipe & Menu Management",font=self.fLG,
                 bg=BG_PANEL,fg=C_WHITE).pack(side=tk.LEFT)

        body=tk.Frame(p,bg=BG_DARK); body.pack(fill=tk.BOTH,expand=True,padx=10,pady=4)
        # ── Left: recipe list ─────────────────────────────────────────────────
        lf=tk.Frame(body,bg=BG_PANEL,width=240)
        lf.pack(side=tk.LEFT,fill=tk.Y,padx=(0,4)); lf.pack_propagate(False)
        tk.Label(lf,text="Recipes",font=self.fBG,bg=BG_PANEL,fg=C_DIM).pack(anchor=tk.W,padx=6,pady=(6,2))
        self.rec_lb=tk.Listbox(lf,font=self.fLB,bg=BG_CARD,fg=C_WHITE,
                               selectbackground=C_GREEN,selectforeground="#000",
                               activestyle="none",bd=0,relief=tk.FLAT,
                               highlightthickness=0)
        self.rec_lb.pack(fill=tk.BOTH,expand=True,padx=4,pady=4)
        for n in RECIPES: self.rec_lb.insert(tk.END,n)
        self.rec_lb.bind("<<ListboxSelect>>",self._rec_select)
        bf=tk.Frame(lf,bg=BG_PANEL); bf.pack(fill=tk.X,padx=4,pady=4)
        tk.Button(bf,text="+ New",font=self.fBG,bg=C_GREEN,fg="#000",relief=tk.FLAT,
                  padx=6,pady=3,command=self._rec_new).pack(side=tk.LEFT,padx=2)
        tk.Button(bf,text="✕ Delete",font=self.fBG,bg=C_RED,fg="#fff",relief=tk.FLAT,
                  padx=6,pady=3,command=self._rec_delete).pack(side=tk.LEFT,padx=2)

        # ── Right: editor ─────────────────────────────────────────────────────
        rf=tk.Frame(body,bg=BG_DARK); rf.pack(side=tk.LEFT,fill=tk.BOTH,expand=True)
        er=tk.Frame(rf,bg=BG_PANEL); er.pack(fill=tk.X,padx=4,pady=4)
        tk.Label(er,text="Recipe Name:",font=self.fLB,bg=BG_PANEL,fg=C_MUTED).pack(side=tk.LEFT,padx=4)
        self.rec_name_var=tk.StringVar()
        tk.Entry(er,textvariable=self.rec_name_var,font=self.fSM,bg=BG_CARD,fg=C_GREEN,
                 insertbackground=C_GREEN,relief=tk.FLAT,width=34).pack(side=tk.LEFT,padx=4)

        tk.Label(rf,text="Ingredients (Station → Weight g):",font=self.fBG,
                 bg=BG_DARK,fg=C_DIM).pack(anchor=tk.W,padx=8,pady=(6,2))
        ing_outer=tk.Frame(rf,bg=BG_DARK); ing_outer.pack(fill=tk.BOTH,expand=True,padx=4)
        self.rec_ing_frame=tk.Frame(ing_outer,bg=BG_DARK)
        self.rec_ing_frame.pack(fill=tk.BOTH,expand=True)
        self._rec_rows=[]   # list of (sid_var, weight_var, row_frame)

        bf2=tk.Frame(rf,bg=BG_DARK); bf2.pack(fill=tk.X,padx=4,pady=6)
        tk.Button(bf2,text="+ Add Ingredient",font=self.fBG,bg=BG_CARD2,fg=C_WHITE,
                  relief=tk.FLAT,padx=6,pady=3,command=self._rec_add_row).pack(side=tk.LEFT,padx=2)
        tk.Button(bf2,text="💾 Save Recipe",font=self.fBT,bg=C_BLUE,fg="#fff",
                  relief=tk.FLAT,padx=10,pady=4,command=self._rec_save).pack(side=tk.RIGHT,padx=4)

    def _rec_select(self,event=None):
        sel=self.rec_lb.curselection()
        if not sel: return
        name=self.rec_lb.get(sel[0])
        self.rec_name_var.set(name)
        # Track which recipe key the editor is currently bound to. Used by
        # _rec_save so a rename can DELETE the old key instead of creating a
        # duplicate entry. None = editing a brand-new recipe.
        self._rec_editing_key=name
        # clear rows
        for _,_,fr in self._rec_rows: fr.destroy()
        self._rec_rows=[]
        for sid,wt in RECIPES.get(name,{}).items():
            self._rec_add_row(sid,wt)

    def _rec_add_row(self,sid=None,wt=0.0):
        fr=tk.Frame(self.rec_ing_frame,bg=BG_CARD2)
        fr.pack(fill=tk.X,padx=2,pady=1)
        sid_var=tk.StringVar(value=str(sid) if sid else "1")
        wt_var=tk.StringVar(value=f"{wt:.1f}" if wt else "")
        sid_choices=[f"{s} – {INGREDIENTS[s]['label']}" for s in sorted(INGREDIENTS)]
        cb=ttk.Combobox(fr,values=sid_choices,font=self.fSM,width=28,state="readonly")
        # Pre-select matching entry
        if sid:
            for i,c in enumerate(sid_choices):
                if c.startswith(str(sid)+" "): cb.current(i); break
        cb.pack(side=tk.LEFT,padx=4,pady=3)
        tk.Entry(fr,textvariable=wt_var,font=self.fSM,bg=BG_DARK,fg=C_GREEN,
                 insertbackground=C_GREEN,relief=tk.FLAT,width=8).pack(side=tk.LEFT,padx=4)
        tk.Button(fr,text="✕",font=self.fBG,bg=C_RED,fg="#fff",relief=tk.FLAT,
                  padx=4,pady=1,command=lambda f=fr:self._rec_del_row(f)).pack(side=tk.LEFT,padx=2)
        self._rec_rows.append((cb,wt_var,fr))

    def _rec_del_row(self,fr):
        self._rec_rows=[r for r in self._rec_rows if r[2]!=fr]
        fr.destroy()

    def _rec_new(self):
        # Tech-gated: Recipe Change tab access already requires PIN, but
        # explicitly guard mutation entry points too in case the tab is
        # unlocked then PIN-revoked mid-session.
        if not self._tech:
            log_msg("Technician PIN required to create recipes","error"); return
        # Clear the editor + bind to "no existing key" so _rec_save creates
        # a fresh recipe instead of renaming whatever was previously open.
        for _,_,fr in self._rec_rows: fr.destroy()
        self._rec_rows=[]
        # Suggest a unique starter name so duplicate creates don't silently
        # overwrite an existing recipe of the same name.
        base="New Recipe"; n=base; i=2
        while n in RECIPES:
            n=f"{base} {i}"; i+=1
        self.rec_name_var.set(n)
        self._rec_editing_key=None
        self._rec_add_row()
        try: self.rec_lb.selection_clear(0,tk.END)
        except Exception: pass

    def _rec_delete(self):
        if not self._tech:
            log_msg("Technician PIN required to delete recipes","error"); return
        sel=self.rec_lb.curselection()
        if not sel:
            self.show_toast("Select a recipe from the list first","warn"); return
        name=self.rec_lb.get(sel[0])
        if name not in RECIPES:
            self.show_toast(f"Recipe '{name}' no longer exists","warn"); return
        if not messagebox.askyesno(
            "Delete Recipe",
            f"Delete recipe '{name}' permanently?\n\nThis cannot be undone."):
            return
        del RECIPES[name]
        save_recipes(RECIPES)
        # Refresh BOTH listboxes (editor list + dashboard selector) AND clear
        # the editor pane so the deleted recipe doesn't visually linger.
        self._rec_refresh_listboxes()
        for _,_,fr in self._rec_rows: fr.destroy()
        self._rec_rows=[]
        self.rec_name_var.set("")
        self._rec_editing_key=None
        log_msg(f"Deleted recipe: {name}","warn")
        self.show_toast(f"Recipe '{name}' deleted.","ok")

    def _rec_refresh_listboxes(self):
        """Re-populate the Recipe Change list AND the Dashboard recipe list
        from the current RECIPES dict. Centralised so every CRUD action
        uses the same path — previously delete missed the editor list and
        save missed the dashboard order, which created stale duplicates.
        """
        names=list(RECIPES.keys())
        try:
            self.rec_lb.delete(0,tk.END)
            for n in names: self.rec_lb.insert(tk.END,n)
        except Exception: pass
        try:
            self.rlb.delete(0,tk.END)
            for n in names: self.rlb.insert(tk.END,n)
        except Exception: pass

    def _rec_save(self):
        # FIX (CRUD): rename used to duplicate (old key remained, new key
        # appeared). Now we track the previously-bound key in
        # self._rec_editing_key and DELETE it on save when the user renamed
        # the recipe. Also: duplicate-name guard, ingredient de-duplication,
        # and a single _rec_refresh_listboxes() path that keeps the editor
        # list and the dashboard order list in sync.
        if not self._tech:
            log_msg("Technician PIN required to save recipes","error"); return
        new_name=self.rec_name_var.get().strip()
        if not new_name:
            log_msg("Recipe name cannot be empty","error")
            self.show_toast("Save failed: recipe name is empty","error")
            return
        weights={}
        for cb,wt_var,_ in self._rec_rows:
            val=cb.get().strip()
            if not val: continue
            try:
                sid=int(val.split(" – ")[0].strip())
                wt=float(wt_var.get()); assert wt>0
            except Exception:
                log_msg("Invalid row — fix station/weight","error")
                self.show_toast("Save failed: bad station/weight on a row","error")
                return
            # De-duplicate: if the user added two rows for the same station,
            # sum their weights so the saved recipe is well-formed JSON.
            weights[sid]=weights.get(sid,0.0)+wt
        if not weights:
            log_msg("Add at least one ingredient","error")
            self.show_toast("Save failed: add at least one ingredient","error")
            return
        old_key=getattr(self,"_rec_editing_key",None)
        is_rename=(old_key is not None and old_key!=new_name and old_key in RECIPES)
        # Block creating a recipe with a name that collides with an EXISTING
        # one (unless we are intentionally updating that same key).
        if new_name in RECIPES and new_name!=old_key:
            if not messagebox.askyesno(
                "Overwrite Recipe",
                f"A recipe named '{new_name}' already exists.\nOverwrite it?"):
                return
        if is_rename:
            # Remove the old entry; the rebuild below adds new_name in its place
            try: del RECIPES[old_key]
            except KeyError: pass
        RECIPES[new_name]=weights
        save_recipes(RECIPES)
        self._rec_refresh_listboxes()
        # Reselect the freshly-saved recipe so the editor stays bound to it
        try:
            idx=list(RECIPES.keys()).index(new_name)
            self.rec_lb.selection_clear(0,tk.END)
            self.rec_lb.selection_set(idx)
            self.rec_lb.see(idx)
        except Exception: pass
        self._rec_editing_key=new_name
        if is_rename:
            log_msg(f"Renamed recipe '{old_key}' → '{new_name}'","ok")
            self.show_toast(f"Recipe renamed: '{old_key}' → '{new_name}'","ok")
        else:
            log_msg(f"Saved recipe: {new_name}","ok")
            self.show_toast(f"Recipe '{new_name}' saved!","ok")

    def _close(self):
        orch.abort(); estop_all()
        set_motor(4,CONVEYOR_CH,0); set_motor(4,MIXER_CH,0)
        for b in boards.values(): b.stop()
        try:
            for b in boards.values():
                if b.ser: b.ser.close()
        except Exception: pass
        self.destroy(); sys.exit(0)

if __name__=="__main__":
    App().mainloop()
