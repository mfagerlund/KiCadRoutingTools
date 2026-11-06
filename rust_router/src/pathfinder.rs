//! Negotiated-congestion (PathFinder) router over KRT's grid obstacle model.
//!
//! McMurchie & Ebeling, "PathFinder: A Negotiation-Based Performance-Driven Router for FPGAs" (1995):
//! every net is routed while overlaps with other nets are allowed but priced; each pass raises the price of
//! shared cells (present congestion, growing each pass) and keeps a history cost on cells that were shared in
//! earlier passes; congested nets are ripped and re-routed until no cell is shared.
//!
//! Model here (grid cells of KRT's GridObstacleMap, through vias only):
//! * Hard obstacles: KRT's base map (permanent copper, board edge, keepouts, nets not being routed) plus every
//!   routed net's own keep-out cells (its pads and stubs, from KRT's per-net obstacle cache): a cell in net B's
//!   keep-out is blocked for every net but B.
//! * Soft (negotiated) obstacles: the copper the nets routed in this run lay down. A net's copper paints two
//!   per-layer counters: `occ_t` (cells a foreign track centreline may not use: radius track + clearance from
//!   its tracks, via/2 + clearance + track/2 from its vias) and `occ_v` (cells a foreign via centre may not use).
//!   A net's own paint is a union, so a cell it covers counts 1 for it; any count above that is a conflict.
//! * Parallelism: the nets to (re)route in one pass are split into batches whose search windows do not overlap;
//!   a batch's searches run on all threads at once and see the same snapshot, which equals routing them one by
//!   one, because no net can reach another's window. The result is deterministic for any thread count.
//!
//! Each net is a Steiner tree: the search grows from the tree (all cells reached so far) to the nearest
//! terminal not yet joined, until every terminal is joined.

use pyo3::prelude::*;
use pyo3::types::PyDict;
use numpy::PyReadonlyArray2;
use rayon::prelude::*;
use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::time::Instant;

use crate::obstacle_map::GridObstacleMap;

const NONE: i32 = -1;
const MULTI: i32 = -2;
const DIRS: [(i32, i32); 8] = [(1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1)];

#[derive(Clone, Copy)]
struct Win {
    x0: i32,
    y0: i32,
    x1: i32, // inclusive
    y1: i32,
}

impl Win {
    fn w(&self) -> i32 { self.x1 - self.x0 + 1 }
    fn h(&self) -> i32 { self.y1 - self.y0 + 1 }
    fn overlaps(&self, o: &Win) -> bool {
        !(self.x1 < o.x0 || o.x1 < self.x0 || self.y1 < o.y0 || o.y1 < self.y0)
    }
    fn contains(&self, x: i32, y: i32) -> bool {
        x >= self.x0 && x <= self.x1 && y >= self.y0 && y <= self.y1
    }
}

struct Net {
    net_id: i64,
    owner: i32,
    terminals: Vec<Vec<(i32, i32, u8)>>,
    centres: Vec<(f32, f32, f32)>, // per terminal: centre x, y and radius (cells) for the heuristic
    bbox: Win,                     // terminals' bounding box
    full: bool,                    // window failed once: search the whole grid
}

#[derive(Clone, Default)]
struct Route {
    paths: Vec<Vec<(i32, i32, u8)>>,
    painted_t: Vec<u32>,
    painted_v: Vec<u32>,
}

#[derive(Clone, Copy, PartialEq)]
struct Open {
    f: f32,
    g: f32,
    idx: u32,
}
impl Eq for Open {}
impl Ord for Open {
    fn cmp(&self, o: &Self) -> Ordering {
        // min-heap on f, ties: larger g first (deeper), then idx for determinism
        o.f.partial_cmp(&self.f).unwrap_or(Ordering::Equal)
            .then_with(|| self.g.partial_cmp(&o.g).unwrap_or(Ordering::Equal))
            .then_with(|| o.idx.cmp(&self.idx))
    }
}
impl PartialOrd for Open {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> { Some(self.cmp(o)) }
}

#[pyclass]
pub struct NegotiatedRouter {
    base: GridObstacleMap,
    x0: i32,
    y0: i32,
    w: i32,
    h: i32,
    nl: usize,
    owner_t: Vec<i32>,
    owner_v: Vec<i32>,
    nets: Vec<Net>,
    routes: Vec<Option<Route>>,
    unroutable: Vec<bool>,
    occ_t: Vec<u16>,
    occ_v: Vec<u16>,
    hist_t: Vec<f32>,
    hist_v: Vec<f32>,
    stamp_t: Vec<u32>,
    stamp_v: Vec<u32>,
    gen: u32,
    // parameters (cells / cost units: one orthogonal cell = 1.0)
    via_cost: f32,
    turn_cost: f32,
    r_tt: f32,
    r_tv: f32,
    r_vv: f32,
    pres0: f32,
    pres_mult: f32,
    hist_fac: f32,
    h_weight: f32,
    margin: i32,
    max_iters: u32,
    threads: usize,
    time_limit_s: f64,
    defer_legalise: bool,
    pres_max: f32, // present-cost cap; history carries on past it
    stop_file: Option<String>, // stop (as on the time limit) once this file exists
    max_expansions: u64,
    chunk: usize, // >0: fixed-size chunks routed together (Jacobi inside a chunk); 0: disjoint-window batches
    log: Vec<String>,
}

impl NegotiatedRouter {
    #[inline]
    fn cell(&self, x: i32, y: i32) -> Option<usize> {
        let dx = x - self.x0;
        let dy = y - self.y0;
        if dx >= 0 && dy >= 0 && dx < self.w && dy < self.h {
            Some(dy as usize * self.w as usize + dx as usize)
        } else {
            None
        }
    }
    #[inline]
    fn plane(&self) -> usize { self.w as usize * self.h as usize }

    #[inline]
    fn track_ok(&self, owner: i32, x: i32, y: i32, l: u8, c: usize) -> bool {
        let o = self.owner_t[l as usize * self.plane() + c];
        if o != NONE && o != owner {
            return false;
        }
        !self.base.is_blocked(x, y, l as usize)
    }
    #[inline]
    fn via_ok(&self, owner: i32, x: i32, y: i32, c: usize) -> bool {
        let o = self.owner_v[c];
        if o != NONE && o != owner {
            return false;
        }
        !self.base.is_via_blocked(x, y)
    }

    fn grid_win(&self) -> Win {
        Win { x0: self.x0, y0: self.y0, x1: self.x0 + self.w - 1, y1: self.y0 + self.h - 1 }
    }

    fn window(&self, n: &Net) -> Win {
        if n.full {
            return self.grid_win();
        }
        let g = self.grid_win();
        Win {
            x0: (n.bbox.x0 - self.margin).max(g.x0),
            y0: (n.bbox.y0 - self.margin).max(g.y0),
            x1: (n.bbox.x1 + self.margin).min(g.x1),
            y1: (n.bbox.y1 + self.margin).min(g.y1),
        }
    }

    /// Steiner-tree search for one net against the current occupancy (its own copper removed). None = no tree.
    fn route_net(&self, ni: usize, pres: f32) -> (Option<Vec<Vec<(i32, i32, u8)>>>, u64) {
        let net = &self.nets[ni];
        let win = self.window(net);
        let (ww, wh) = (win.w() as usize, win.h() as usize);
        let nl = self.nl;
        let wplane = ww * wh;
        let total = wplane * nl;
        let lidx = |x: i32, y: i32, l: u8| -> usize {
            l as usize * wplane + (y - win.y0) as usize * ww + (x - win.x0) as usize
        };
        let mut g = vec![f32::INFINITY; total];
        let mut parent = vec![u32::MAX; total];
        let mut dir = vec![255u8; total];
        let mut closed = vec![0u32; total];
        let mut goal = vec![0u16; total]; // terminal index + 1
        let mut tree_mark = vec![false; total];
        let mut generation = 0u32;
        let mut expansions = 0u64;

        let nt = net.terminals.len();
        for (ti, t) in net.terminals.iter().enumerate() {
            for &(x, y, l) in t {
                if win.contains(x, y) && (l as usize) < nl {
                    goal[lidx(x, y, l)] = (ti + 1) as u16;
                }
            }
        }
        let is_term = |i: usize| goal[i] != 0;
        let mut joined = vec![false; nt];
        joined[0] = true;
        let mut tree: Vec<usize> = Vec::new();
        for &(x, y, l) in &net.terminals[0] {
            if win.contains(x, y) && (l as usize) < nl {
                let i = lidx(x, y, l);
                if !tree_mark[i] {
                    tree_mark[i] = true;
                    tree.push(i);
                }
            }
        }
        if tree.is_empty() {
            return (None, 0);
        }
        let mut paths: Vec<Vec<(i32, i32, u8)>> = Vec::new();
        let decode = |i: usize| -> (i32, i32, u8) {
            let l = i / wplane;
            let r = i % wplane;
            (win.x0 + (r % ww) as i32, win.y0 + (r / ww) as i32, l as u8)
        };
        let plane = self.plane();
        let pres_t = |c: usize, l: u8| -> f32 {
            let k = l as usize * plane + c;
            (1.0 + self.hist_t[k]) * (1.0 + pres * self.occ_t[k] as f32)
        };

        for _round in 1..nt {
            generation += 1;
            // heuristic targets: the terminals not joined yet
            let targets: Vec<(f32, f32, f32)> = (0..nt).filter(|&t| !joined[t]).map(|t| net.centres[t]).collect();
            let hfun = |x: i32, y: i32| -> f32 {
                let mut best = f32::INFINITY;
                for &(cx, cy, r) in &targets {
                    let dx = (x as f32 - cx).abs();
                    let dy = (y as f32 - cy).abs();
                    let (a, b) = if dx > dy { (dx, dy) } else { (dy, dx) };
                    let d = (a - b) + 1.41421356 * b - r;
                    if d < best {
                        best = d;
                    }
                }
                best.max(0.0) * self.h_weight
            };
            let mut heap = BinaryHeap::new();
            for &i in &tree {
                g[i] = 0.0;
                parent[i] = u32::MAX;
                dir[i] = 255;
                let (x, y, _) = decode(i);
                heap.push(Open { f: hfun(x, y), g: 0.0, idx: i as u32 });
            }
            // closed[] semantics: closed[i] == generation*2 means closed; == generation*2-1 means g valid
            for &i in &tree {
                closed[i] = generation * 2 - 1;
            }
            let mut found: Option<usize> = None;
            while let Some(Open { g: gc, idx, .. }) = heap.pop() {
                let i = idx as usize;
                if closed[i] == generation * 2 {
                    continue;
                }
                if gc > g[i] {
                    continue;
                }
                closed[i] = generation * 2;
                expansions += 1;
                if expansions > self.max_expansions {
                    return (None, expansions);
                }
                let t = goal[i];
                if t != 0 && !joined[(t - 1) as usize] {
                    found = Some(i);
                    break;
                }
                let (x, y, l) = decode(i);
                let d0 = dir[i];
                // planar moves
                for (di, &(dx, dy)) in DIRS.iter().enumerate() {
                    let nx = x + dx;
                    let ny = y + dy;
                    if !win.contains(nx, ny) {
                        continue;
                    }
                    let j = lidx(nx, ny, l);
                    if closed[j] == generation * 2 {
                        continue;
                    }
                    let c = match self.cell(nx, ny) { Some(c) => c, None => continue };
                    if !is_term(j) && !tree_mark[j] && !self.track_ok(net.owner, nx, ny, l, c) {
                        continue;
                    }
                    let len = if dx != 0 && dy != 0 { 1.41421356 } else { 1.0 };
                    let mut step = len * pres_t(c, l);
                    if d0 != 255 && d0 != di as u8 {
                        step += self.turn_cost;
                    }
                    let ng = gc + step;
                    let valid = closed[j] == generation * 2 - 1;
                    if !valid || ng < g[j] {
                        g[j] = ng;
                        parent[j] = i as u32;
                        dir[j] = di as u8;
                        closed[j] = generation * 2 - 1;
                        heap.push(Open { f: ng + hfun(nx, ny), g: ng, idx: j as u32 });
                    }
                }
                // via: through via to every other layer
                if nl > 1 {
                    let c = match self.cell(x, y) { Some(c) => c, None => continue };
                    if self.via_ok(net.owner, x, y, c) {
                        let mut occ = 0u16;
                        for ll in 0..nl {
                            occ = occ.max(self.occ_v[ll * plane + c]);
                        }
                        let vstep = self.via_cost * (1.0 + self.hist_v[c]) * (1.0 + pres * occ as f32);
                        for l2 in 0..nl as u8 {
                            if l2 == l {
                                continue;
                            }
                            let j = lidx(x, y, l2);
                            if closed[j] == generation * 2 {
                                continue;
                            }
                            if !is_term(j) && !tree_mark[j] && !self.track_ok(net.owner, x, y, l2, c) {
                                continue;
                            }
                            let ng = gc + vstep;
                            let valid = closed[j] == generation * 2 - 1;
                            if !valid || ng < g[j] {
                                g[j] = ng;
                                parent[j] = i as u32;
                                dir[j] = 255;
                                closed[j] = generation * 2 - 1;
                                heap.push(Open { f: ng + hfun(x, y), g: ng, idx: j as u32 });
                            }
                        }
                    }
                }
            }
            let end = match found { Some(e) => e, None => return (None, expansions) };
            let ti = (goal[end] - 1) as usize;
            joined[ti] = true;
            // path back to the tree
            let mut p = vec![end];
            let mut cur = end;
            while parent[cur] != u32::MAX {
                cur = parent[cur] as usize;
                p.push(cur);
            }
            p.reverse();
            for &i in &p {
                if !tree_mark[i] {
                    tree_mark[i] = true;
                    tree.push(i);
                }
            }
            for &(x, y, l) in &net.terminals[ti] {
                if win.contains(x, y) && (l as usize) < nl {
                    let i = lidx(x, y, l);
                    if !tree_mark[i] {
                        tree_mark[i] = true;
                        tree.push(i);
                    }
                }
            }
            if p.len() > 1 {
                paths.push(p.into_iter().map(decode).collect());
            }
        }
        (Some(paths), expansions)
    }

    fn disk(r: f32) -> Vec<(i32, i32)> {
        let ri = r.ceil() as i32;
        let mut v = Vec::new();
        for dy in -ri..=ri {
            for dx in -ri..=ri {
                if ((dx * dx + dy * dy) as f32) < r * r {
                    v.push((dx, dy));
                }
            }
        }
        if v.is_empty() {
            v.push((0, 0));
        }
        v
    }

    /// Paint a route's copper into occ_t / occ_v (union per net); returns the painted cell lists.
    fn paint(&mut self, paths: &[Vec<(i32, i32, u8)>], dt: &[(i32, i32)], dtv: &[(i32, i32)], dvv: &[(i32, i32)]) -> Route {
        self.gen = self.gen.wrapping_add(1);
        if self.gen == 0 {
            self.stamp_t.iter_mut().for_each(|s| *s = 0);
            self.stamp_v.iter_mut().for_each(|s| *s = 0);
            self.gen = 1;
        }
        let gen = self.gen;
        let plane = self.plane();
        let nl = self.nl;
        let mut pt = Vec::new();
        let mut pv = Vec::new();
        let mut mark = |arr: &mut Vec<u16>, stamp: &mut Vec<u32>, out: &mut Vec<u32>, k: usize| {
            if stamp[k] != gen {
                stamp[k] = gen;
                arr[k] = arr[k].saturating_add(1);
                out.push(k as u32);
            }
        };
        let (x0, y0, w, h) = (self.x0, self.y0, self.w, self.h);
        let cellf = |x: i32, y: i32| -> Option<usize> {
            let dx = x - x0;
            let dy = y - y0;
            if dx >= 0 && dy >= 0 && dx < w && dy < h { Some(dy as usize * w as usize + dx as usize) } else { None }
        };
        for p in paths {
            for (k, &(x, y, l)) in p.iter().enumerate() {
                let is_via = k + 1 < p.len() && p[k + 1].0 == x && p[k + 1].1 == y && p[k + 1].2 != l;
                for &(dx, dy) in dt {
                    if let Some(c) = cellf(x + dx, y + dy) {
                        mark(&mut self.occ_t, &mut self.stamp_t, &mut pt, l as usize * plane + c);
                    }
                }
                for &(dx, dy) in dtv {
                    if let Some(c) = cellf(x + dx, y + dy) {
                        mark(&mut self.occ_v, &mut self.stamp_v, &mut pv, l as usize * plane + c);
                    }
                }
                if is_via {
                    for ll in 0..nl {
                        for &(dx, dy) in dtv {
                            if let Some(c) = cellf(x + dx, y + dy) {
                                mark(&mut self.occ_t, &mut self.stamp_t, &mut pt, ll * plane + c);
                            }
                        }
                        for &(dx, dy) in dvv {
                            if let Some(c) = cellf(x + dx, y + dy) {
                                mark(&mut self.occ_v, &mut self.stamp_v, &mut pv, ll * plane + c);
                            }
                        }
                    }
                }
            }
        }
        Route { paths: paths.to_vec(), painted_t: pt, painted_v: pv }
    }

    fn unpaint(&mut self, r: &Route) {
        for &k in &r.painted_t {
            self.occ_t[k as usize] = self.occ_t[k as usize].saturating_sub(1);
        }
        for &k in &r.painted_v {
            self.occ_v[k as usize] = self.occ_v[k as usize].saturating_sub(1);
        }
    }

    /// Conflicting cells of a routed net: (track cells k=l*plane+c, via positions c).
    fn conflicts(&self, r: &Route) -> (Vec<usize>, Vec<usize>) {
        let plane = self.plane();
        let mut ct = Vec::new();
        let mut cv = Vec::new();
        for p in &r.paths {
            for (k, &(x, y, l)) in p.iter().enumerate() {
                let c = match self.cell(x, y) { Some(c) => c, None => continue };
                let kt = l as usize * plane + c;
                if self.occ_t[kt] > 1 {
                    ct.push(kt);
                }
                let is_via = k + 1 < p.len() && p[k + 1].0 == x && p[k + 1].1 == y && p[k + 1].2 != l;
                if is_via {
                    let over = (0..self.nl).any(|ll| self.occ_v[ll * plane + c] > 1);
                    if over {
                        cv.push(c);
                    }
                }
            }
        }
        (ct, cv)
    }

    fn batches(&self, order: &[usize]) -> Vec<Vec<usize>> {
        let pad = (self.r_vv.max(self.r_tt).max(self.r_tv)).ceil() as i32 + 1;
        let mut out: Vec<(Vec<usize>, Vec<Win>)> = Vec::new();
        for &ni in order {
            let w0 = self.window(&self.nets[ni]);
            let w = Win { x0: w0.x0 - pad, y0: w0.y0 - pad, x1: w0.x1 + pad, y1: w0.y1 + pad };
            let mut placed = false;
            for (members, wins) in out.iter_mut() {
                if wins.len() < 4096 && !wins.iter().any(|o| o.overlaps(&w)) {
                    members.push(ni);
                    wins.push(w);
                    placed = true;
                    break;
                }
            }
            if !placed {
                out.push((vec![ni], vec![w]));
            }
        }
        out.into_iter().map(|(m, _)| m).collect()
    }
}

#[pymethods]
impl NegotiatedRouter {
    #[new]
    #[pyo3(signature = (base, x0, y0, w, h))]
    pub fn new(base: PyRef<GridObstacleMap>, x0: i32, y0: i32, w: i32, h: i32) -> PyResult<Self> {
        if w <= 0 || h <= 0 {
            return Err(pyo3::exceptions::PyValueError::new_err("empty grid"));
        }
        let nl = base.num_layers;
        let plane = w as usize * h as usize;
        Ok(Self {
            base: base.clone_fresh(),
            x0, y0, w, h, nl,
            owner_t: vec![NONE; plane * nl],
            owner_v: vec![NONE; plane],
            nets: Vec::new(),
            routes: Vec::new(),
            unroutable: Vec::new(),
            occ_t: vec![0; plane * nl],
            occ_v: vec![0; plane * nl],
            hist_t: vec![0.0; plane * nl],
            hist_v: vec![0.0; plane],
            stamp_t: vec![0; plane * nl],
            stamp_v: vec![0; plane * nl],
            gen: 0,
            via_cost: 50.0,
            turn_cost: 0.3,
            r_tt: 3.0,
            r_tv: 4.0,
            r_vv: 5.0,
            pres0: 0.5,
            pres_mult: 1.5,
            hist_fac: 0.3,
            h_weight: 1.0,
            margin: 60,
            max_iters: 60,
            threads: 0,
            time_limit_s: 600.0,
            stop_file: None,
            defer_legalise: false,
            pres_max: 30.0,
            max_expansions: 20_000_000,
            chunk: 16,
            log: Vec::new(),
        })
    }

    /// Keep-out cells of one owner (a net's pads and stubs): blocked for every other owner.
    pub fn add_keepout(&mut self, owner: i32, cells: PyReadonlyArray2<i32>, vias: PyReadonlyArray2<i32>) {
        let plane = self.plane();
        for row in cells.as_array().rows() {
            let (x, y, l) = (row[0], row[1], row[2]);
            if l < 0 || l as usize >= self.nl {
                continue;
            }
            if let Some(c) = self.cell(x, y) {
                let k = l as usize * plane + c;
                let o = self.owner_t[k];
                self.owner_t[k] = if o == NONE || o == owner { owner } else { MULTI };
            }
        }
        for row in vias.as_array().rows() {
            if let Some(c) = self.cell(row[0], row[1]) {
                let o = self.owner_v[c];
                self.owner_v[c] = if o == NONE || o == owner { owner } else { MULTI };
            }
        }
    }

    /// A net to route: its terminals, each a list of (gx, gy, layer) cells. Returns its index.
    pub fn add_net(&mut self, net_id: i64, owner: i32, terminals: Vec<Vec<(i32, i32, u8)>>) -> PyResult<usize> {
        // a terminal keeps its first cell (the pad centre) and every other cell the net may stand on
        let terms: Vec<Vec<(i32, i32, u8)>> = terminals.into_iter().map(|t| {
            t.iter().enumerate().filter(|&(k, &(x, y, l))| {
                (l as usize) < self.nl && (k == 0 || self.cell(x, y).map_or(false, |c| self.track_ok(owner, x, y, l, c)))
            }).map(|(_, &c)| c).collect::<Vec<_>>()
        }).filter(|t: &Vec<(i32, i32, u8)>| !t.is_empty()).collect();
        if terms.len() < 2 {
            return Err(pyo3::exceptions::PyValueError::new_err("a net needs two terminals"));
        }
        let mut bb = Win { x0: i32::MAX, y0: i32::MAX, x1: i32::MIN, y1: i32::MIN };
        let mut centres = Vec::new();
        for t in &terms {
            let (mut sx, mut sy) = (0.0f32, 0.0f32);
            for &(x, y, _) in t {
                bb.x0 = bb.x0.min(x);
                bb.y0 = bb.y0.min(y);
                bb.x1 = bb.x1.max(x);
                bb.y1 = bb.y1.max(y);
                sx += x as f32;
                sy += y as f32;
            }
            let n = t.len() as f32;
            let (cx, cy) = (sx / n, sy / n);
            let r = t.iter().map(|&(x, y, _)| {
                let dx = x as f32 - cx;
                let dy = y as f32 - cy;
                (dx * dx + dy * dy).sqrt()
            }).fold(0.0f32, f32::max);
            centres.push((cx, cy, r));
        }
        self.nets.push(Net { net_id, owner, terminals: terms, centres, bbox: bb, full: false });
        self.routes.push(None);
        self.unroutable.push(false);
        Ok(self.nets.len() - 1)
    }

    #[pyo3(signature = (via_cost=None, turn_cost=None, r_tt=None, r_tv=None, r_vv=None, pres0=None, pres_mult=None,
                        hist_fac=None, h_weight=None, margin=None, max_iters=None, threads=None, time_limit_s=None,
                        max_expansions=None, chunk=None, stop_file=None,
                        defer_legalise=None, pres_max=None))]
    pub fn set_params(&mut self, via_cost: Option<f32>, turn_cost: Option<f32>, r_tt: Option<f32>, r_tv: Option<f32>,
                      r_vv: Option<f32>, pres0: Option<f32>, pres_mult: Option<f32>, hist_fac: Option<f32>,
                      h_weight: Option<f32>, margin: Option<i32>, max_iters: Option<u32>, threads: Option<usize>,
                      time_limit_s: Option<f64>, max_expansions: Option<u64>, chunk: Option<usize>,
                      stop_file: Option<String>, defer_legalise: Option<bool>,
                      pres_max: Option<f32>) {
        if let Some(v) = pres_max { self.pres_max = v; }
        if let Some(v) = defer_legalise { self.defer_legalise = v; }
        if let Some(v) = stop_file { self.stop_file = if v.is_empty() { None } else { Some(v) }; }
        if let Some(v) = chunk { self.chunk = v; }
        if let Some(v) = via_cost { self.via_cost = v; }
        if let Some(v) = turn_cost { self.turn_cost = v; }
        if let Some(v) = r_tt { self.r_tt = v; }
        if let Some(v) = r_tv { self.r_tv = v; }
        if let Some(v) = r_vv { self.r_vv = v; }
        if let Some(v) = pres0 { self.pres0 = v; }
        if let Some(v) = pres_mult { self.pres_mult = v; }
        if let Some(v) = hist_fac { self.hist_fac = v; }
        if let Some(v) = h_weight { self.h_weight = v; }
        if let Some(v) = margin { self.margin = v; }
        if let Some(v) = max_iters { self.max_iters = v; }
        if let Some(v) = threads { self.threads = v; }
        if let Some(v) = time_limit_s { self.time_limit_s = v; }
        if let Some(v) = max_expansions { self.max_expansions = v; }
    }

    /// Negotiate until no conflict, the pass limit or the time limit; then drop the most conflicted nets until
    /// the rest is legal. Returns stats.
    pub fn run<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let t0 = Instant::now();
        let n = self.nets.len();
        let dt = Self::disk(self.r_tt);
        let dtv = Self::disk(self.r_tv);
        let dvv = Self::disk(self.r_vv);
        let pool = rayon::ThreadPoolBuilder::new().num_threads(self.threads).build()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
        // first pass order: smallest bounding box first
        let mut order: Vec<usize> = (0..n).collect();
        order.sort_by_key(|&i| {
            let b = self.nets[i].bbox;
            ((b.x1 - b.x0) + (b.y1 - b.y0), i)
        });
        let mut pres = self.pres0;
        let mut iters = 0u32;
        let mut total_exp = 0u64;
        let mut searches = 0u64;
        let mut todo = order.clone();
        let mut last_conflicts = usize::MAX;
        let mut stopped = "converged";
        loop {
            iters += 1;
            let mut resolved = 0usize;
            let mut cut = false; // the time limit or the stop file cut this pass short
            let it_t = Instant::now();
            let batches: Vec<Vec<usize>> = if self.chunk > 0 {
                // nets in one chunk do not see each other: a fresh (seeded) order every pass, so two nets that
                // collide are not routed blind to each other again and again
                if iters > 1 {
                    let mut s: u64 = 0x9E3779B97F4A7C15 ^ (iters as u64).wrapping_mul(0xBF58476D1CE4E5B9);
                    for i in (1..todo.len()).rev() {
                        s ^= s << 13;
                        s ^= s >> 7;
                        s ^= s << 17;
                        todo.swap(i, (s % (i as u64 + 1)) as usize);
                    }
                }
                todo.chunks(self.chunk).map(|c| c.to_vec()).collect()
            } else {
                self.batches(&todo)
            };
            let nb = batches.len();
            for batch in batches {
                if self.out_of_time(&t0) {
                    cut = true;
                    break;
                }
                let batch_t = Instant::now();
                for &ni in &batch {
                    if let Some(r) = self.routes[ni].take() {
                        self.unpaint(&r);
                    }
                }
                // Searches run in parallel against the state before the round; results commit in order, and a route
                // that chunk-mates committed earlier in the round now cross goes to the next round, searched again
                // (in parallel with the other stale ones) against them. The first result of a round always commits,
                // so rounds end; two conflicting nets in one chunk no longer swap the same corridor pass after pass.
                let mut retry = Vec::new();
                let mut round: Vec<usize> = batch.clone();
                let mut first_round = true;
                while !round.is_empty() {
                    let me: &Self = self;
                    let timed: Vec<(usize, Option<Vec<Vec<(i32, i32, u8)>>>, u64, usize, f64)> = py.allow_threads(|| {
                        pool.install(|| {
                            round.par_iter().map(|&ni| {
                                let st = Instant::now();
                                let (p, e) = me.route_net(ni, pres);
                                let c0 = p.as_ref().map_or(0, |paths| me.foreign_hits(paths));
                                (ni, p, e, c0, st.elapsed().as_secs_f64())
                            }).collect()
                        })
                    });
                    for (ni, _, e, _, s) in &timed {
                        if *s > 5.0 {
                            let net = &self.nets[*ni];
                            eprintln!("  negotiated pass {iters}: net {} ({} terminals, window {}) took {s:.1} s, {e} expansions",
                                      net.net_id, net.terminals.len(), if net.full { "full" } else { "bbox" });
                        }
                    }
                    let found: Vec<(usize, Option<Vec<Vec<(i32, i32, u8)>>>, u64, usize)> =
                        timed.into_iter().map(|(a, b, c, d, _)| (a, b, c, d)).collect();
                    if !first_round {
                        resolved += round.len();
                    }
                    first_round = false;
                    let mut stale = Vec::new();
                    for (k, (ni, p, e, c0)) in found.into_iter().enumerate() {
                        total_exp += e;
                        searches += 1;
                        match p {
                            Some(paths) => {
                                if k > 0 && self.foreign_hits(&paths) > c0 {
                                    stale.push(ni);
                                    continue;
                                }
                                let r = self.paint(&paths, &dt, &dtv, &dvv);
                                self.routes[ni] = Some(r);
                            }
                            None => {
                                if !self.nets[ni].full {
                                    self.nets[ni].full = true;
                                    retry.push(ni);
                                } else {
                                    self.unroutable[ni] = true;
                                }
                            }
                        }
                    }
                    round = stale;
                }
                let bt = batch_t.elapsed().as_secs_f64();
                if bt > 2.0 {
                    eprintln!("  negotiated pass {iters}: batch of {} nets took {bt:.1} s", batch.len());
                }
                // a window failure retries once on the whole grid (alone: its window overlaps everything)
                for ni in retry {
                    let (p, e) = py.allow_threads(|| self.route_net(ni, pres));
                    total_exp += e;
                    searches += 1;
                    match p {
                        Some(paths) => {
                            let r = self.paint(&paths, &dt, &dtv, &dvv);
                            self.routes[ni] = Some(r);
                        }
                        None => self.unroutable[ni] = true,
                    }
                }
            }
            // conflicts and history
            let mut conflicted = Vec::new();
            let mut ncells = 0usize;
            let mut hist_t_add = Vec::new();
            let mut hist_v_add = Vec::new();
            for ni in 0..n {
                if let Some(r) = &self.routes[ni] {
                    let (ct, cv) = self.conflicts(r);
                    if !ct.is_empty() || !cv.is_empty() {
                        ncells += ct.len() + cv.len();
                        conflicted.push(ni);
                        hist_t_add.extend(ct);
                        hist_v_add.extend(cv);
                    }
                }
            }
            for k in hist_t_add {
                self.hist_t[k] += self.hist_fac;
            }
            for c in hist_v_add {
                self.hist_v[c] += self.hist_fac;
            }
            let routed = self.routes.iter().filter(|r| r.is_some()).count();
            let line = format!(
                "pass {iters}: {} nets in {nb} batches, {routed}/{n} routed, {} unroutable, {} conflicted nets ({ncells} cells), {resolved} re-searched, pres {pres:.2}, {:.2} s",
                todo.len(), self.unroutable.iter().filter(|&&u| u).count(), conflicted.len(), it_t.elapsed().as_secs_f64());
            eprintln!("  negotiated {line}");
            self.log.push(line);
            if cut {
                stopped = "time limit";
                break;
            }
            if conflicted.is_empty() {
                break;
            }
            if iters >= self.max_iters {
                stopped = "pass limit";
                break;
            }
            if self.out_of_time(&t0) {
                stopped = "time limit";
                break;
            }
            let _ = last_conflicts;
            last_conflicts = ncells;
            pres = (pres * self.pres_mult).min(self.pres_max);  // history carries on past the cap
            todo = conflicted;
        }
        // legalize: drop the most conflicted net until no conflict remains (KRT_NEGOTIATED_DUMP defers it)
        let dropped = if self.defer_legalise { Vec::new() } else { self.legalise_inner() };
        let d = PyDict::new(py);
        d.set_item("passes", iters)?;
        d.set_item("stopped", stopped)?;
        d.set_item("seconds", t0.elapsed().as_secs_f64())?;
        d.set_item("expansions", total_exp)?;
        d.set_item("searches", searches)?;
        d.set_item("routed", self.routes.iter().filter(|r| r.is_some()).count())?;
        d.set_item("unroutable", self.nets.iter().zip(&self.unroutable).filter(|(_, &u)| u).map(|(n, _)| n.net_id).collect::<Vec<_>>())?;
        d.set_item("dropped", dropped)?;
        d.set_item("log", self.log.clone())?;
        Ok(d)
    }

    /// The routed paths of net index i (each a list of (gx, gy, layer)); None when it has no legal route.
    pub fn paths(&self, i: usize) -> Option<Vec<Vec<(i32, i32, u8)>>> {
        self.routes.get(i).and_then(|r| r.as_ref().map(|r| r.paths.clone()))
    }

    pub fn net_count(&self) -> usize { self.nets.len() }

    /// Drop the most conflicted nets until the rest is legal (after run() with defer_legalise); their net ids.
    pub fn legalise(&mut self) -> Vec<i64> { self.legalise_inner() }

    /// Net index i's conflicting cells now: track centreline cells (x, y, layer) inside a foreign keep-out, via
    /// cells (x, y) inside a foreign via keep-out (diagnosis).
    pub fn conflict_cells(&self, i: usize) -> (Vec<(i32, i32, u8)>, Vec<(i32, i32)>) {
        let r = match self.routes.get(i).and_then(|r| r.as_ref()) { Some(r) => r, None => return (vec![], vec![]) };
        let (ct, cv) = self.conflicts(r);
        let plane = self.plane();
        let w = self.w as usize;
        let xy = |c: usize| (self.x0 + (c % w) as i32, self.y0 + (c / w) as i32);
        (ct.into_iter().map(|k| { let (x, y) = xy(k % plane); (x, y, (k / plane) as u8) }).collect(),
         cv.into_iter().map(xy).collect())
    }
}

impl NegotiatedRouter {
    /// Cells of an unpainted route inside another net's keep-out now: track centreline cells with any occupancy,
    /// via cells with any via occupancy on some layer.
    fn foreign_hits(&self, paths: &[Vec<(i32, i32, u8)>]) -> usize {
        let plane = self.plane();
        let mut k = 0;
        for p in paths {
            for (i, &(x, y, l)) in p.iter().enumerate() {
                let c = match self.cell(x, y) { Some(c) => c, None => continue };
                if self.occ_t[l as usize * plane + c] > 0 {
                    k += 1;
                }
                if i + 1 < p.len() && p[i + 1].0 == x && p[i + 1].1 == y && p[i + 1].2 != l
                    && (0..self.nl).any(|ll| self.occ_v[ll * plane + c] > 0) {
                    k += 1;
                }
            }
        }
        k
    }

    fn legalise_inner(&mut self) -> Vec<i64> {
        let n = self.nets.len();
        let mut dropped = Vec::new();
        loop {
            let mut worst: Option<(usize, usize)> = None;
            for ni in 0..n {
                if let Some(r) = &self.routes[ni] {
                    let (ct, cv) = self.conflicts(r);
                    let k = ct.len() + cv.len();
                    if k > 0 && worst.map_or(true, |(_, wk)| k > wk) {
                        worst = Some((ni, k));
                    }
                }
            }
            match worst {
                None => break,
                Some((ni, _)) => {
                    let r = self.routes[ni].take().unwrap();
                    self.unpaint(&r);
                    dropped.push(self.nets[ni].net_id);
                }
            }
        }
        dropped
    }

    fn out_of_time(&self, t0: &Instant) -> bool {
        t0.elapsed().as_secs_f64() > self.time_limit_s
            || self.stop_file.as_ref().map_or(false, |p| std::path::Path::new(p).exists())
    }
}
