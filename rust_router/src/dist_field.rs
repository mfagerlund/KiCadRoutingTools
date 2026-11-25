//! Target distance field: an exact cost-to-go lower-bound heuristic for A*.
//!
//! The octile heuristic ignores obstacles, so a weighted A* (h_weight > 1)
//! that meets a wall or a dense pocket between source and target floods the
//! whole pocket before it turns around; on the dense benchmark boards most
//! capped full searches are that flood. This module computes, once per
//! search, the shortest-path cost from every cell of a window around the
//! terminals to the nearest target over a PERMISSIVE copy of the A* move
//! graph:
//!   * same-layer 8-neighbour steps into cells `is_blocked` does not block
//!     (track clearance margin, direction rules, via exclusion and every soft
//!     cost are ignored, so each step here is no more expensive than A*'s);
//!   * layer changes at cells where a via is legal (or a free via sits),
//!     onto non-forbidden layers whose cell is free, at `via_cost` (0 at a
//!     free via).
//! Step costs are A*'s base costs scaled by the layer's cost, plus the
//! per-step proximity estimate the octile heuristic already adds. The field
//! is computed backwards from the targets with Dial's bucket queue (integer
//! edge costs) and read by the search as `max(octile, field)`.
//!
//! Cells outside the window or not reached inside it read None (the search
//! falls back to the octile heuristic there).

use crate::obstacle_map::GridObstacleMap;

pub(crate) struct DistField {
    x0: i32,
    y0: i32,
    w: i32,
    h: i32,
    layers: usize,
    dist: Vec<u32>,
    /// Some source cell was reached (false: no path inside the window).
    pub source_reached: bool,
    pub cells: usize,
}

pub(crate) struct FieldCosts {
    /// Per-layer orthogonal / diagonal step cost.
    pub ortho: Vec<u32>,
    pub diag: Vec<u32>,
    pub via: u32,
    pub forbidden: Vec<bool>,
    pub via_rung: usize,
}

const INF: u32 = u32::MAX;

impl DistField {
    #[inline]
    pub fn get(&self, gx: i32, gy: i32, layer: usize) -> Option<u32> {
        let dx = gx.wrapping_sub(self.x0);
        let dy = gy.wrapping_sub(self.y0);
        if (dx as u32) < (self.w as u32) && (dy as u32) < (self.h as u32) && layer < self.layers {
            let d = self.dist[(layer * self.h as usize + dy as usize) * self.w as usize + dx as usize];
            if d != INF { Some(d) } else { None }
        } else {
            None
        }
    }

    /// Build the field. `margin` cells are added around the terminals' bbox;
    /// None when the window would exceed `max_nodes` (cells x layers).
    pub fn build(obstacles: &GridObstacleMap, costs: &FieldCosts,
                 sources: &[(i32, i32, u8)], targets: &[(i32, i32, u8)],
                 margin: i32, max_nodes: usize) -> Option<DistField> {
        if targets.is_empty() || sources.is_empty() {
            return None;
        }
        let layers = obstacles.num_layers;
        let (mut lx, mut ly, mut hx, mut hy) = (i32::MAX, i32::MAX, i32::MIN, i32::MIN);
        for &(x, y, _) in sources.iter().chain(targets.iter()) {
            lx = lx.min(x); ly = ly.min(y); hx = hx.max(x); hy = hy.max(y);
        }
        let x0 = lx - margin;
        let y0 = ly - margin;
        let w = (hx - lx + 1 + 2 * margin) as i64;
        let h = (hy - ly + 1 + 2 * margin) as i64;
        let nodes = w * h * layers as i64;
        if w <= 0 || h <= 0 || nodes as u64 > max_nodes as u64 {
            return None;
        }
        let (w, h) = (w as i32, h as i32);
        let plane = (w as usize) * (h as usize);
        let mut dist = vec![INF; plane * layers];
        let idx = |x: i32, y: i32, l: usize| -> Option<usize> {
            let dx = x - x0;
            let dy = y - y0;
            if dx >= 0 && dy >= 0 && dx < w && dy < h {
                Some(l * plane + dy as usize * w as usize + dx as usize)
            } else {
                None
            }
        };
        // Free-cell cache: 0 unknown, 1 free, 2 blocked (is_blocked does a
        // hash probe for blocked cells; each cell is asked up to 8 times).
        let mut free = vec![0u8; plane * layers];
        let mut is_free = |i: usize, x: i32, y: i32, l: usize| -> bool {
            match free[i] {
                1 => true,
                2 => false,
                _ => {
                    let f = !costs.forbidden[l] && !obstacles.is_blocked(x, y, l);
                    free[i] = if f { 1 } else { 2 };
                    f
                }
            }
        };

        let max_edge = costs.ortho.iter().chain(costs.diag.iter()).copied()
            .max().unwrap_or(1).max(costs.via) as usize;
        let nb = max_edge + 1;
        let mut buckets: Vec<Vec<u32>> = vec![Vec::new(); nb];
        let mut pending = 0usize;
        for &(x, y, l) in targets {
            if let Some(i) = idx(x, y, l as usize) {
                if dist[i] != 0 {
                    dist[i] = 0;
                    buckets[0].push(i as u32);
                    pending += 1;
                }
            }
        }
        const DIRS: [(i32, i32, bool); 8] = [
            (1, 0, false), (-1, 0, false), (0, 1, false), (0, -1, false),
            (1, 1, true), (1, -1, true), (-1, 1, true), (-1, -1, true),
        ];
        let mut d: u32 = 0;
        let mut cells = 0usize;
        while pending > 0 {
            let b = d as usize % nb;
            while let Some(i) = buckets[b].pop() {
                pending -= 1;
                let i = i as usize;
                if dist[i] != d {
                    continue; // stale
                }
                cells += 1;
                let l = i / plane;
                let r = i % plane;
                let x = x0 + (r % w as usize) as i32;
                let y = y0 + (r / w as usize) as i32;
                for &(ddx, ddy, diag) in DIRS.iter() {
                    let (nx, ny) = (x + ddx, y + ddy);
                    if let Some(j) = idx(nx, ny, l) {
                        let nd = d + if diag { costs.diag[l] } else { costs.ortho[l] };
                        if nd < dist[j] && is_free(j, nx, ny, l) {
                            dist[j] = nd;
                            buckets[nd as usize % nb].push(j as u32);
                            pending += 1;
                        }
                    }
                }
                // Layer change at this cell (the reverse of a via onto `l`):
                // legal where a via may be placed or a free via sits.
                let free_via = obstacles.is_free_via(x, y);
                if layers > 1 && (free_via || !obstacles.is_via_blocked_rung(x, y, costs.via_rung)) {
                    let nd = d + if free_via { 0 } else { costs.via };
                    for l2 in 0..layers {
                        if l2 == l {
                            continue;
                        }
                        let j = l2 * plane + r;
                        if nd < dist[j] && is_free(j, x, y, l2) {
                            dist[j] = nd;
                            buckets[nd as usize % nb].push(j as u32);
                            pending += 1;
                        }
                    }
                }
            }
            d += 1;
        }
        let source_reached = sources.iter().any(|&(x, y, l)| {
            idx(x, y, l as usize).map_or(false, |i| dist[i] != INF)
        });
        Some(DistField { x0, y0, w, h, layers, dist, source_reached, cells })
    }
}
