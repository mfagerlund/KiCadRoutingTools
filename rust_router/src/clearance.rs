//! Foreign-copper clearance kernels for the Python cleanup passes (single_ended_routing._seg_foreign_seg_dist).
//!
//! `seg_dist_need` is the `need` path of _seg_foreign_seg_dist in one call: the 5 mm window over the layer's foreign
//! rows, the exact segment-to-segment lower bound per row, and the 0.02 mm sampled sweep over the rows whose bound is
//! below need + 1e-9. Every float operation is numpy's, in numpy's order (np.linspace's i * (1 / n) samples with the
//! last set to 1.0, np.clip as min(max(v, 0), 1), np.hypot as hypot), so the value below need is the numpy value
//! bit for bit; KRT_RUST_SEG_CHECK=1 on the Python side compares the two on every call (2026-11-09: numpy's per-call
//! overhead was most of cm5io's 48 s smoothing clears()).

use numpy::PyReadonlyArray1;
use pyo3::prelude::*;

#[inline]
fn clip01(v: f64) -> f64 {
    v.max(0.0).min(1.0)
}

#[pyfunction]
#[pyo3(signature = (x1, y1, x2, y2, net_id, nid, ax, ay, bx, by, hw, minx, maxx, miny, maxy, exempt, excess_tab, need, window, n))]
#[allow(clippy::too_many_arguments)]
pub fn seg_dist_need(
    x1: f64, y1: f64, x2: f64, y2: f64, net_id: i64,
    nid: PyReadonlyArray1<i64>,
    ax: PyReadonlyArray1<f64>, ay: PyReadonlyArray1<f64>, bx: PyReadonlyArray1<f64>, by: PyReadonlyArray1<f64>,
    hw: PyReadonlyArray1<f64>,
    minx: PyReadonlyArray1<f64>, maxx: PyReadonlyArray1<f64>, miny: PyReadonlyArray1<f64>, maxy: PyReadonlyArray1<f64>,
    exempt: Option<PyReadonlyArray1<bool>>,
    excess_tab: Option<PyReadonlyArray1<f64>>,
    need: f64, window: f64, n: usize,
) -> PyResult<f64> {
    let nid = nid.as_slice()?;
    let (ax, ay, bx, by, hw) = (ax.as_slice()?, ay.as_slice()?, bx.as_slice()?, by.as_slice()?, hw.as_slice()?);
    let (minx, maxx, miny, maxy) = (minx.as_slice()?, maxx.as_slice()?, miny.as_slice()?, maxy.as_slice()?);
    let ex = match &exempt { Some(e) => Some(e.as_slice()?), None => None };
    let tab = match &excess_tab { Some(t) => Some(t.as_slice()?), None => None };
    let m = nid.len();
    if m == 0 {
        return Ok(1e9);
    }
    let lox = x1.min(x2) - window;
    let hix = x1.max(x2) + window;
    let loy = y1.min(y2) - window;
    let hiy = y1.max(y2) + window;
    let ux = x2 - x1;
    let uy = y2 - y1;
    let ul2 = ux * ux + uy * uy;
    let thr = need + 1e-9;
    // rows: (index, excess) of the windowed foreign rows whose lower bound is below thr
    let mut rows: Vec<(usize, f64)> = Vec::new();
    let mut any_near = false;
    let mut lb_min = f64::INFINITY;
    for k in 0..m {
        if !(maxx[k] >= lox && minx[k] <= hix && maxy[k] >= loy && miny[k] <= hiy && nid[k] != net_id) {
            continue;
        }
        if let Some(e) = ex {
            if k < e.len() && e[k] {
                continue;
            }
        }
        any_near = true;
        let (a_x, a_y, b_x, b_y) = (ax[k], ay[k], bx[k], by[k]);
        let abx = b_x - a_x;
        let aby = b_y - a_y;
        let l2 = abx * abx + aby * aby;
        let safe = if l2 > 0.0 { l2 } else { 1.0 };
        let p2ab = |px: f64, py: f64| -> f64 {
            let pax = px - a_x;
            let pay = py - a_y;
            let tt = if l2 > 0.0 { clip01((pax * abx + pay * aby) / safe) } else { 0.0 };
            (px - (a_x + tt * abx)).hypot(py - (a_y + tt * aby))
        };
        let mut lb = p2ab(x1, y1).min(p2ab(x2, y2));
        if ul2 > 0.0 {
            for (qx, qy) in [(a_x, a_y), (b_x, b_y)] {
                let tt = clip01(((qx - x1) * ux + (qy - y1) * uy) / ul2);
                lb = lb.min((qx - (x1 + tt * ux)).hypot(qy - (y1 + tt * uy)));
            }
            let c1 = ux * (a_y - y1) - uy * (a_x - x1);
            let c2 = ux * (b_y - y1) - uy * (b_x - x1);
            let c3 = abx * (y1 - a_y) - aby * (x1 - a_x);
            let c4 = abx * (y2 - a_y) - aby * (x2 - a_x);
            if c1 * c2 < 0.0 && c3 * c4 < 0.0 {
                lb = 0.0;
            }
        }
        let exs = match tab {
            Some(t) => {
                let f = nid[k];
                if f >= 0 && (f as usize) < t.len() { t[f as usize] } else { 0.0 }
            }
            None => 0.0,
        };
        let lbv = lb - hw[k] - exs;
        if lbv < lb_min {
            lb_min = lbv;
        }
        if lbv < thr {
            rows.push((k, exs));
        }
    }
    if !any_near {
        return Ok(1e9);
    }
    if rows.is_empty() {
        return Ok(lb_min);
    }
    // np.linspace(0, 1, n + 1): i * (1 / n), the last sample exactly 1 (n from the caller: Python's math.hypot)
    let step = 1.0 / (n as f64);
    let mut best = f64::INFINITY;
    for i in 0..=n {
        let t = if i == n { 1.0 } else { (i as f64) * step + 0.0 };
        let sx = x1 + (x2 - x1) * t;
        let sy = y1 + (y2 - y1) * t;
        for &(k, exs) in &rows {
            let (a_x, a_y) = (ax[k], ay[k]);
            let abx = bx[k] - a_x;
            let aby = by[k] - a_y;
            let l2 = abx * abx + aby * aby;
            let safe = if l2 > 0.0 { l2 } else { 1.0 };
            let pax = sx - a_x;
            let pay = sy - a_y;
            let tt = if l2 > 0.0 { clip01((pax * abx + pay * aby) / safe) } else { 0.0 };
            let px = a_x + tt * abx;
            let py = a_y + tt * aby;
            let mut d = (sx - px).hypot(sy - py) - hw[k];
            if tab.is_some() {
                d = d - exs;
            }
            if d < best {
                best = d;
            }
        }
    }
    Ok(best)
}

/// `pad_dist_need` is the `need` path of _seg_foreign_pad_dist over the layer's rounded-rect pads (the custom-polygon
/// pads stay in Python): the window by each pad's global half-extent, the segment-to-centre lower bound minus the
/// pad's radius (and its excess), and the sampled rounded-rect distance in each pad's local frame over the pads whose
/// bound is below need + 1e-9. Excess = max(local_clearance - base, 0), then max with the net-class table, only when
/// `base` is given (numpy's order). Returns 1e9 when no pad is in the window.
#[pyfunction]
#[pyo3(signature = (x1, y1, x2, y2, net_id, nids, cx, cy, hx, hy, cr, rc, rs, ex, ey, plc, base, excess_tab, need, window, n))]
#[allow(clippy::too_many_arguments)]
pub fn pad_dist_need(
    x1: f64, y1: f64, x2: f64, y2: f64, net_id: i64,
    nids: PyReadonlyArray1<i64>,
    cx: PyReadonlyArray1<f64>, cy: PyReadonlyArray1<f64>, hx: PyReadonlyArray1<f64>, hy: PyReadonlyArray1<f64>,
    cr: PyReadonlyArray1<f64>, rc: PyReadonlyArray1<f64>, rs: PyReadonlyArray1<f64>,
    ex: PyReadonlyArray1<f64>, ey: PyReadonlyArray1<f64>, plc: PyReadonlyArray1<f64>,
    base: Option<f64>,
    excess_tab: Option<PyReadonlyArray1<f64>>,
    need: f64, window: f64, n: usize,
) -> PyResult<f64> {
    let nids = nids.as_slice()?;
    let (cx, cy, hx, hy, cr) = (cx.as_slice()?, cy.as_slice()?, hx.as_slice()?, hy.as_slice()?, cr.as_slice()?);
    let (rc, rs, ex, ey, plc) = (rc.as_slice()?, rs.as_slice()?, ex.as_slice()?, ey.as_slice()?, plc.as_slice()?);
    let tab = match &excess_tab { Some(t) => Some(t.as_slice()?), None => None };
    let m = nids.len();
    let lox = x1.min(x2) - window;
    let hix = x1.max(x2) + window;
    let loy = y1.min(y2) - window;
    let hiy = y1.max(y2) + window;
    let vx = x2 - x1;
    let vy = y2 - y1;
    let l2 = vx * vx + vy * vy;
    let thr = need + 1e-9;
    let mut rows: Vec<(usize, f64)> = Vec::new();
    let mut any_near = false;
    let mut lb_min = f64::INFINITY;
    for k in 0..m {
        if !(cx[k] + ex[k] >= lox && cx[k] - ex[k] <= hix && cy[k] + ey[k] >= loy && cy[k] - ey[k] <= hiy
            && nids[k] != net_id) {
            continue;
        }
        any_near = true;
        let mut lb = if l2 > 0.0 {
            let t = clip01(((cx[k] - x1) * vx + (cy[k] - y1) * vy) / l2);
            (cx[k] - (x1 + t * vx)).hypot(cy[k] - (y1 + t * vy))
        } else {
            (cx[k] - x1).hypot(cy[k] - y1)
        };
        lb = lb - ((hx[k] - cr[k]).max(0.0).hypot((hy[k] - cr[k]).max(0.0)) + cr[k]);
        let mut exs = 0.0;
        if let Some(b) = base {
            exs = (plc[k] - b).max(0.0);
            if let Some(t) = tab {
                let f = nids[k];
                let v = if f >= 0 && (f as usize) < t.len() { t[f as usize] } else { 0.0 };
                exs = exs.max(v);
            }
            lb = lb - exs;
        }
        if lb < lb_min {
            lb_min = lb;
        }
        if lb < thr {
            rows.push((k, exs));
        }
    }
    if !any_near {
        return Ok(1e9);
    }
    if rows.is_empty() {
        return Ok(lb_min);
    }
    let step = 1.0 / (n as f64);
    let mut best = f64::INFINITY;
    for i in 0..=n {
        let t = if i == n { 1.0 } else { (i as f64) * step + 0.0 };
        let sx = x1 + (x2 - x1) * t;
        let sy = y1 + (y2 - y1) * t;
        for &(k, exs) in &rows {
            let ddx = sx - cx[k];
            let ddy = sy - cy[k];
            let lx = (ddx * rc[k] + ddy * rs[k]).abs();
            let ly = (-ddx * rs[k] + ddy * rc[k]).abs();
            let dx = (lx - (hx[k] - cr[k])).max(0.0);
            let dy = (ly - (hy[k] - cr[k])).max(0.0);
            let mut d = dx.hypot(dy) - cr[k];
            if base.is_some() {
                d = d - exs;
            }
            if d < best {
                best = d;
            }
        }
    }
    Ok(best)
}
