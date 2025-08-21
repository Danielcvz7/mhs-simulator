# app.py — MHS Calculator (per-order lanes, workers/lane, cell gap, changeovers)
# Run locally:  streamlit run app.py

import math
import pandas as pd
import numpy as np
import streamlit as st

# ---------- Streamlit page config ----------
st.set_page_config(page_title="MHS Calculator (Per-Order)", layout="wide")

# ---------- Constraints ----------
MAX_INBOUND_DOORS = 6
MAX_OUTBOUND_DOORS = 7

PNA_LIMIT_PER_LANE_CPM = 19     # Print & Apply per lane limit
PNA_MERGE_LIMIT_CPM    = 40     # Merge zone global cap
INCHSTORE_LIMIT_PER_LANE = 42   # InchStore ceiling per lane
INDUCT_WORKER_CPM      = 12     # Induction worker capacity (cases/min/worker) per lane
SORT_WORKER_CPM        = 5      # Sort/pallet build worker capacity (cases/min) - global in this version

MIN_CELL_GAP_IN        = 3.0    # inches (hard minimum)

# Forklifts (per your rule: only one active for unload and one active for load)
FORKLIFT_PALLETS_PER_MIN = 1.0  # pallets/min for the single active forklift


# ---------- Utility functions ----------
def cell_rate_per_lane_cpm(cell_speed_fpm: float, box_len_in: float, gap_in: float) -> float:
    """cases/min/lane from the cell: speed / ft-per-case, with gap minimum enforced."""
    gap = max(gap_in, MIN_CELL_GAP_IN)
    ft_per_case = (box_len_in + gap) / 12.0
    if ft_per_case <= 0:
        return 0.0
    return cell_speed_fpm / ft_per_case


def changeover_minutes(lanes_used: int) -> int:
    """
    Your rule:
      - If lanes <= 2  → +2 minutes
      - If lanes >= 4  → +7 minutes
      - If lanes == 3  → +4 minutes (explicit assumption)
    """
    if lanes_used <= 2:
        return 2
    if lanes_used >= 4:
        return 7
    return 4


# ---------- UI Header ----------
st.title("📦 MHS Calculator — Per-Order Lanes/Workers/Gap + Changeovers")
st.markdown(
    "- **Per order** you set: *Inbound Lanes Used*, *Workers per Lane*, and *Cell Gap (in)*.\n"
    "- Selecting a **Commodity** auto-fills **Box Length (in)** and **Cell Speed (ft/min)**.\n"
    "- **Changeovers** between orders: ≤2 lanes → **+2 min**, 3 lanes → **+4 min**, ≥4 lanes → **+7 min**.\n"
    "- Flow honored: **Unload pallets → Stage @ P&A station → Same cases flow to P&A merge zone**."
)

# ---------- Commodity Catalog ----------
with st.expander("📚 Edit Commodity Catalog (name → length & cell speed)", expanded=True):
    default_comm = pd.DataFrame(
        [
            {"Commodity": "Oysters", "Length_in": 22.5, "CellSpeed_fpm": 13.0},
            # Add more rows or edit in place
        ],
        columns=["Commodity", "Length_in", "CellSpeed_fpm"]
    )
    comm_df = st.data_editor(
        default_comm,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "Commodity": st.column_config.TextColumn(),
            "Length_in": st.column_config.NumberColumn(step=0.1, help="Box length (inches)"),
            "CellSpeed_fpm": st.column_config.NumberColumn(step=0.1, help="Cell belt speed (ft/min)"),
        },
        key="comm_editor_per_order"
    )

# Fast lookups
comm_to_len  = dict(zip(comm_df["Commodity"], comm_df["Length_in"]))
comm_to_cspd = dict(zip(comm_df["Commodity"], comm_df["CellSpeed_fpm"]))

# ---------- Orders Input (Per-order lanes/workers/gap) ----------
st.subheader("🧾 Orders (per order: lanes used, workers per lane, cell gap)")
orders_default = pd.DataFrame(
    [
        {
            "Customer": "La imperial", "Commodity": "Oysters",
            "Pallets": 10, "CasesPerPallet": 50,
            "InboundLanesUsed": 3, "WorkersPerLane": 1, "CellGap_in": 3.0
        },
        {
            "Customer": "Youngstown", "Commodity": "Oysters",
            "Pallets": 20, "CasesPerPallet": 220,
            "InboundLanesUsed": 5, "WorkersPerLane": 1, "CellGap_in": 3.0
        },
        {
            "Customer": "Sweet Seasons", "Commodity": "Oysters",
            "Pallets": 15, "CasesPerPallet": 120,
            "InboundLanesUsed": 2, "WorkersPerLane": 1, "CellGap_in": 3.0
        },
    ],
    columns=[
        "Customer", "Commodity", "Pallets", "CasesPerPallet",
        "InboundLanesUsed", "WorkersPerLane", "CellGap_in"
    ]
)

orders = st.data_editor(
    orders_default,
    num_rows="dynamic",
    use_container_width=True,
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

# ---------- Sidebar: global sort workers ----------
with st.sidebar:
    st.header("Global")
    sort_workers = st.number_input("# Workers at sort/pallet build (global)", min_value=1, max_value=50, value=6)
    st.caption("Sort stacking cap = workers × 5 cpm (global). "
               "We can make this per order later if you prefer.")

# ---------- Per-order compute ----------
def compute_order_row(row: pd.Series) -> dict:
    customer = str(row["Customer"])
    comm     = str(row["Commodity"])
    pallets  = int(row["Pallets"])
    cpp      = int(row["CasesPerPallet"])
    lanes    = int(row["InboundLanesUsed"])
    wpl      = int(row["WorkersPerLane"])
    gap_in   = float(row["CellGap_in"])

    total_cases = pallets * cpp
    length_in   = float(comm_to_len.get(comm, 22.5))
    cspd_fpm    = float(comm_to_cspd.get(comm, 13.0))

    # Per-lane feed and P&A rate
    feed_per_lane_cpm = wpl * INDUCT_WORKER_CPM
    pna_lane_cpm      = min(feed_per_lane_cpm, PNA_LIMIT_PER_LANE_CPM)
    pna_total_cpm     = lanes * pna_lane_cpm

    # Cell rate per lane and inchstore ceiling per lane
    cell_lane_cpm     = cell_rate_per_lane_cpm(cspd_fpm, length_in, gap_in)
    lane_after_cell   = min(cell_lane_cpm, INCHSTORE_LIMIT_PER_LANE)
    post_cell_total   = lanes * lane_after_cell

    # Upstream cap before sort
    upstream_cap_cpm  = min(pna_total_cpm, post_cell_total, PNA_MERGE_LIMIT_CPM)

    # Downstream sort/pallet-build capacity (global)
    sort_cap_cpm      = sort_workers * SORT_WORKER_CPM

    # Overall system cap
    system_cap_cpm    = min(upstream_cap_cpm, sort_cap_cpm)
    system_cap_cph    = system_cap_cpm * 60.0

    # Times
    t_offload_min     = pallets / FORKLIFT_PALLETS_PER_MIN
    t_process_min     = total_cases / max(system_cap_cpm, 1e-9)
    t_load_min        = pallets / FORKLIFT_PALLETS_PER_MIN

    # Cell span (first-in → last-in) limited by min(lanes*cell_lane_cpm, merge cap)
    t_cell_span_min   = total_cases / max(min(lanes * cell_lane_cpm, PNA_MERGE_LIMIT_CPM), 1e-9)

    # Efficiencies (display only)
    pna_eff           = (pna_lane_cpm / PNA_LIMIT_PER_LANE_CPM) if PNA_LIMIT_PER_LANE_CPM > 0 else 0.0
    pna_merge_eff     = (min(pna_total_cpm, PNA_MERGE_LIMIT_CPM) / PNA_MERGE_LIMIT_CPM) if PNA_MERGE_LIMIT_CPM > 0 else 0.0
    treat_eff         = (lane_after_cell / max(cell_lane_cpm, 1e-9)) if cell_lane_cpm > 0 else 0.0

    # Changeover before next order
    chg_min           = changeover_minutes(lanes)

    return {
        "Customer": customer,
        "Commodity": comm,
        "Pallets": pallets,
        "Cases x Pallet": cpp,
        "Total Cases": total_cases,
        "Inbound Lanes Used": lanes,
        "Workers / Lane": wpl,
        "Cell Gap (in)": round(gap_in, 1),

        "PnA Rate (cpm/lane)": round(pna_lane_cpm, 2),
        "PnA Eff %": f"{round(pna_eff * 100.0, 1)}%",
        "PnA Merge Rate (cpm)": round(min(pna_total_cpm, PNA_MERGE_LIMIT_CPM), 2),
        "PnA Merge Eff %": f"{round(pna_merge_eff * 100.0, 1)}%",

        "Treatment rate (cpm/lane)": round(cell_lane_cpm, 2),
        "Treatment eff %": f"{round(treat_eff * 100.0, 1)}%",
        "ECP Crossfire %": "—",

        # Per your note: inducted at PnA = reach PnA merge
        "Cases inducted @ PnA": total_cases,
        "Cases @ PnA Merge": total_cases,

        "Time to offload (min)": round(t_offload_min, 1),
        "Time through MHS (min)": round(t_process_min, 1),
        "Time to load (min)": round(t_load_min, 1),
        "Cell first→last (min)": round(t_cell_span_min, 1),

        "Changeover to Next (min)": chg_min,
        "System bottleneck (cpm)": round(system_cap_cpm, 2),
        "System throughput (cases/hr)": round(system_cap_cph, 1),
    }


# Build per-order results
order_rows = []
for _, r in orders.iterrows():
    order_rows.append(compute_order_row(r))
out_df = pd.DataFrame(order_rows)

# Timeline (serial orders with changeovers)
timeline_records = []
t_clock = 0.0
for idx, r in out_df.iterrows():
    t_start = t_clock
    t_order = (
        float(r["Time to offload (min)"]) +
        float(r["Time through MHS (min)"]) +
        float(r["Time to load (min)"])
    )
    t_finish = t_start + t_order
    timeline_records.append({
        "Order #": int(idx + 1),
        "Customer": r["Customer"],
        "Start (min)": round(t_start, 1),
        "Finish (min)": round(t_finish, 1)
    })
    # Add changeover before next order
    t_clock = t_finish + float(r["Changeover to Next (min)"])

timeline_df = pd.DataFrame(timeline_records)
total_runtime_min = float(timeline_df["Finish (min)"].iloc[-1]) if not timeline_df.empty else 0.0

# Summary cards
tot_pallets = int(orders["Pallets"].sum())
tot_cases = int((orders["Pallets"] * orders["CasesPerPallet"]).sum())

st.markdown("### 📈 Summary")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total Pallets", f"{tot_pallets:,}")
c2.metric("Total Cases", f"{tot_cases:,}")
c3.metric("Number of Orders", f"{len(orders)}")
c4.metric("End-to-end Time (incl. changeovers)", f"{int(total_runtime_min // 60)}h {int(total_runtime_min % 60)}m")

# Output tables
st.markdown("### 🧮 Per-Order Results")
display_cols = [
    "Customer", "Commodity", "Pallets", "Cases x Pallet", "Total Cases",
    "Inbound Lanes Used", "Workers / Lane", "Cell Gap (in)",
    "PnA Rate (cpm/lane)", "PnA Eff %", "PnA Merge Rate (cpm)", "PnA Merge Eff %",
    "Treatment rate (cpm/lane)", "Treatment eff %", "ECP Crossfire %",
    "Cases inducted @ PnA", "Cases @ PnA Merge",
    "Time to offload (min)", "Time through MHS (min)", "Time to load (min)", "Cell first→last (min)",
    "Changeover to Next (min)",
    "System bottleneck (cpm)", "System throughput (cases/hr)"
]
st.dataframe(out_df[display_cols], use_container_width=True)

st.markdown("### 🗓️ Timeline (serial with changeovers)")
st.dataframe(timeline_df, use_container_width=True)

st.caption(
    "Constraints enforced: 1 active forklift for unload and 1 for load (1 pallet/min each); "
    "6 inbound & 7 outbound doors; P&A per lane ≤19 cpm; P&A merge ≤40 cpm (global); "
    "InchStore ≤42 cpm per lane; induction worker = 12 cpm/worker/lane; "
    "sort worker = 5 cpm/worker (global); cell rate = CellSpeed / ((Length + Gap)/12) with Gap ≥ 3 in. "
    "Changeovers: ≤2 lanes → +2 min, 3 lanes → +4 min, ≥4 lanes → +7 min."
)
