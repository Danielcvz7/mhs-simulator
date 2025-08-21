Python 3.11.9 (tags/v3.11.9:de54cf5, Apr  2 2024, 10:12:12) [MSC v.1938 64 bit (AMD64)] on win32
Type "help", "copyright", "credits" or "license()" for more information.
# app.py — MHS Calculator (per-order lanes, workers/lane, gap, and changeovers)
import math
import numpy as np
import pandas as pd
import streamlit as st

st.set_page_config("MHS Calculator (Per-Order)", layout="wide")

# =========================
# CONSTANTS (Constraints)
# =========================
MAX_INBOUND_DOORS  = 6
MAX_OUTBOUND_DOORS = 7

PNA_LIMIT_PER_LANE_CPM   = 19   # station limit per lane
PNA_MERGE_LIMIT_CPM      = 40   # merge zone limit (global)
INCHSTORE_LIMIT_PER_LANE = 42   # per lane
INDUCT_WORKER_CPM        = 12   # cases/min/worker at induction
SORT_WORKER_CPM          = 5    # cases/min/worker at sort (global in this version)

MIN_CELL_GAP_IN          = 3.0  # inches (hard minimum)

# Forklift simplification per your note: only one can unload and load at a time
FORKLIFT_PALLETS_PER_MIN = 1.0  # pallets/min for the single active forklift

# =========================
# Commodity catalog
# =========================
st.title("📦 MHS Calculator — Per-Order Lanes/Workers/Gap + Changeovers")

st.markdown(
    "- **Per order** you set: *Inbound Lanes Used*, *Workers per Lane*, and *Cell Gap (in)*.\n"
    "- Selecting a **Commodity** auto-fills **Length** and **Cell Speed**.\n"
    "- **Changeovers** between orders: lanes ≤2 → **+2 min**; lanes =3 → **+4 min** (assumption); lanes ≥4 → **+7 min**.\n"
    "- Flow honored: **Unload → Stage @ P&A station → Same cases flow to P&A merge zone**."
)

with st.expander("📚 Edit Commodity Catalog", expanded=True):
    default_comm = pd.DataFrame(
        [
            {"Commodity":"Oysters", "Length_in":22.5, "CellSpeed_fpm":13.0},
            # Add more commodities here or via the editor
        ],
        columns=["Commodity","Length_in","CellSpeed_fpm"]
    )
    comm_df = st.data_editor(
        default_comm, num_rows="dynamic", use_container_width=True,
        column_config={
            "Commodity": st.column_config.TextColumn(),
            "Length_in": st.column_config.NumberColumn(step=0.1),
            "CellSpeed_fpm": st.column_config.NumberColumn(step=0.1),
        },
        key="comm_editor_per_order"
    )

comm_to_len  = dict(zip(comm_df["Commodity"], comm_df["Length_in"]))
comm_to_cspd = dict(zip(comm_df["Commodity"], comm_df["CellSpeed_fpm"]))

# =========================
# Orders input (Per order lanes/workers/gap)
# =========================
st.subheader("🧾 Orders (Per Order: lanes, workers per lane, cell gap)")

orders_default = pd.DataFrame(
    [
        {
            "Customer":"La imperial", "Commodity":"Oysters",
            "Pallets":10, "CasesPerPallet":50,
            "InboundLanesUsed":3, "WorkersPerLane":1, "CellGap_in":3.0
        },
        {
            "Customer":"Youngstown", "Commodity":"Oysters",
            "Pallets":20, "CasesPerPallet":220,
            "InboundLanesUsed":5, "WorkersPerLane":1, "CellGap_in":3.0
        },
        {
            "Customer":"Sweet Seasons", "Commodity":"Oysters",
            "Pallets":15, "CasesPerPallet":120,
            "InboundLanesUsed":2, "WorkersPerLane":1, "CellGap_in":3.0
        },
    ],
    columns=[
        "Customer","Commodity","Pallets","CasesPerPallet",
        "InboundLanesUsed","WorkersPerLane","CellGap_in"
    ]
)

orders = st.data_editor(
    orders_default, num_rows="dynamic", use_container_width=True,
    column_config={
        "Commodity": st.column_config.SelectboxColumn(options=sorted(comm_df["Commodity"].unique())),
        "Pallets": st.column_config.NumberColumn(step=1, min_value=0),
        "CasesPerPallet": st.column_config.NumberColumn(step=1, min_value=0),
        "InboundLanesUsed": st.column_config.NumberColumn(step=1, min_value=1, max_value=6),
        "WorkersPerLane": st.column_config.NumberColumn(step=1, min_value=1, max_value=10),
        "CellGap_in": st.column_config.NumberColumn(step=0.5, min_value=MIN_CELL_GAP_IN),
    },
    key="orders_editor_per_order"
)

# =========================
# Global knobs that are still relevant
# =========================
with st.sidebar:
    st.header("Global (kept simple)")
    # Only 1 forklift can unload and 1 can load at a time (constraint) → no input needed
    sort_workers = st.number_input("# Workers at sort/pallet build (global)", 1, 40, 6)
    st.caption("Sort stacking cap = workers × 5 cpm (global). "
               "Make this per-order later if you want.")

# =========================
# Derived helpers
# =========================
def cell_rate_per_lane_cpm(cell_speed_fpm, box_len_in, gap_in):
    gap_in = max(gap_in, MIN_CELL_GAP_IN)
    ft_per_case = (box_len_in + gap_in)/12.0
    if ft_per_case <= 0: return 0.0
    return cell_speed_fpm / ft_per_case

def changeover_minutes(lanes_used: int) -> int:
    if lanes_used <= 2: return 2
    if lanes_used >= 4: return 7
    return 4  # lanes == 3 (assumption per your note)

# =========================
# Per-order calculation
# =========================
def compute_order_row(row):
    customer = row["Customer"]
    comm     = row["Commodity"]
    pallets  = int(row["Pallets"])
    cpp      = int(row["CasesPerPallet"])
    lanes    = int(row["InboundLanesUsed"])
    wpl      = int(row["WorkersPerLane"])
    gap_in   = float(row["CellGap_in"])

    total_cases = pallets * cpp
    length_in   = float(comm_to_len.get(comm, 22.5))
    cspd_fpm    = float(comm_to_cspd.get(comm, 13.0))

    # P&A per lane limited by both worker feed and station limit
    feed_per_lane = wpl * INDUCT_WORKER_CPM
    pna_lane_cpm  = min(feed_per_lane, PNA_LIMIT_PER_LANE_CPM)
    pna_total_cpm = lanes * pna_lane_cpm

    # Cell lane rate
    cell_lane_cpm = cell_rate_per_lane_cpm(cspd_fpm, length_in, gap_in)
    # Inchstore per lane limit
    lane_after_cell_cpm = min(cell_lane_cpm, INCHSTORE_LIMIT_PER_LANE)
    # Upstream total from lanes after inchstore+cell
    post_cell_total_cpm = lanes * lane_after_cell_cpm

    # Merge cap (global)
    upstream_cap_cpm = min(pna_total_cpm, post_cell_total_cpm, PNA_MERGE_LIMIT_CPM)

    # Downstream sort stacking cap (global)
    sort_cap_cpm = sort_workers * SORT_WORKER_CPM

    # Final system cap
    system_cap_cpm = min(upstream_cap_cpm, sort_cap_cpm)
    system_cap_cph = system_cap_cpm * 60.0

    # Times
    # Unload time (1 pallet/min forklift)
    t_offload_min = pallets / FORKLIFT_PALLETS_PER_MIN
    # Processing time (limited by bottleneck cap)
    t_mhs_min     = total_cases / max(system_cap_cpm, 1e-9)
    # Outbound load time
    t_load_min    = pallets / FORKLIFT_PALLETS_PER_MIN
    # Cell span (first-in to last-in): limited by min(lanes*cell_lane_cpm, merge cap)
    t_cell_span   = total_cases / max(min(lanes*cell_lane_cpm, PNA_MERGE_LIMIT_CPM), 1e-9)

    # Efficiencies (informational)
    pna_eff       = pna_lane_cpm / PNA_LIMIT_PER_LANE_CPM if PNA_LIMIT_PER_LANE_CPM>0 else 0
    pna_merge_eff = min(pna_total_cpm, PNA_MERGE_LIMIT_CPM) / PNA_MERGE_LIMIT_CPM if PNA_MERGE_LIMIT_CPM>0 else 0
    treat_eff     = lane_after_cell_cpm / max(cell_lane_cpm, 1e-9) if cell_lane_cpm>0 else 0

    return {
        "Customer": customer,
        "Commodity": comm,
        "Pallets": pallets,
        "Cases x Pallet": cpp,
        "Total Cases": total_cases,
        "Inbound Lanes Used": lanes,
        "Workers / Lane": wpl,
        "Cell Gap (in)": round(gap_in,1),

        "PnA Rate (cpm/lane)": round(pna_lane_cpm,2),
        "PnA Eff %": f"{round(pna_eff*100,1)}%",
        "PnA Merge Rate (cpm)": round(min(pna_total_cpm, PNA_MERGE_LIMIT_CPM),2),
        "PnA Merge Eff %": f"{round(pna_merge_eff*100,1)}%",

        "Treatment rate (cpm/lane)": round(cell_lane_cpm,2),
        "Treatment eff %": f"{round(treat_eff*100,1)}%",
        "ECP Crossfire %": "—",  # not modeled here

        "Cases inducted @ PnA": total_cases,   # same cases reach merge zone (per your rule)
        "Cases @ PnA Merge": total_cases,

        "Time to offload (min)": round(t_offload_min,1),
        "Time through MHS (min)": round(t_mhs_min,1),
        "Time to load (min)": round(t_load_min,1),
        "Cell first→last (min)": round(t_cell_span,1),

        "System bottleneck (cpm)": round(system_cap_cpm,2),
        "System throughput (cases/hr)": round(system_cap_cph,1),

        # Changeover to next order:
        "Changeover to Next (min)": changeover_minutes(lanes),
    }

# Compute per order
rows = [compute_order_row(r) for _, r in orders.iterrows()]
out_df = pd.DataFrame(rows)

# Build a timeline with cumulative changeovers (serial orders in listed order)
timeline = []
t_clock = 0.0
for i, r in out_df.iterrows():
    t_start = t_clock
    t_order = r["Time to offload (min)"] + r["Time through MHS (min)"] + r["Time to load (min)"]
    t_end   = t_start + t_order
    timeline.append({"Order#": i+1, "Customer": r["Customer"], "Start (min)": round(t_start,1), "Finish (min)": round(t_end,1)})
    # add changeover before next order
    t_clock = t_end + r["Changeover to Next (min)"]

timeline_df = pd.DataFrame(timeline)
total_runtime_min = timeline_df["Finish (min)"].iloc[-1] if not timeline_df.empty else 0.0

# =========================
# Friendly summary
# =========================
... tot_pallets = int(orders["Pallets"].sum())
... tot_cases   = int((orders["Pallets"]*orders["CasesPerPallet"]).sum())
... 
... st.markdown("### 📈 Summary")
... c1,c2,c3,c4 = st.columns(4)
... c1.metric("Total Pallets", f"{tot_pallets:,}")
... c2.metric("Total Cases", f"{tot_cases:,}")
... c3.metric("Orders", f"{len(orders)}")
... c4.metric("End-to-end Time (incl. changeovers)", f"{int(total_runtime_min//60)}h {int(total_runtime_min%60)}m")
... 
... # =========================
... # Output tables
... # =========================
... st.markdown("### 🧮 Per-Order Results")
... show_cols = [
...     "Customer","Commodity","Pallets","Cases x Pallet","Total Cases",
...     "Inbound Lanes Used","Workers / Lane","Cell Gap (in)",
...     "PnA Rate (cpm/lane)","PnA Eff %","PnA Merge Rate (cpm)","PnA Merge Eff %",
...     "Treatment rate (cpm/lane)","Treatment eff %","ECP Crossfire %",
...     "Cases inducted @ PnA","Cases @ PnA Merge",
...     "Time to offload (min)","Time through MHS (min)","Time to load (min)","Cell first→last (min)",
...     "Changeover to Next (min)",
...     "System bottleneck (cpm)","System throughput (cases/hr)"
... ]
... st.dataframe(out_df[show_cols], use_container_width=True)
... 
... st.markdown("### 🗓️ Timeline (serial execution with changeovers)")
... st.dataframe(timeline_df, use_container_width=True)
... 
... st.caption(
...     "Notes: Only one forklift active for unload and load (1 pallet/min each). "
...     "P&A lane ≤19 cpm; P&A merge ≤40 cpm (global). InchStore ≤42 cpm/lane. "
...     "Induction worker feed = 12 cpm per worker per lane. "
...     "Cell rate per lane = CellSpeed_fpm / ((Length + Gap)/12), Gap ≥ 3 in. "
...     "Changeover rule: lanes ≤2 → +2 min; lanes =3 → +4 min (assumption); lanes ≥4 → +7 min."
... )
