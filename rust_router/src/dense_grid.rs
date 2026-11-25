//! Windowed dense i32 mirror of a sparse per-cell cost map.
//!
//! The A* step reads the stub proximity map and the per-layer track proximity
//! maps for every expanded neighbour; those reads were about a third of the
//! search time (two hash probes per neighbour, most of them misses). This is
//! a PURE MIRROR of such a map: the hash map stays authoritative and is the
//! only thing hashed, iterated, counted or fingerprinted; every writer of the
//! map writes the same value here right after, and every clear clears both.
//!
//! The window grows lazily to cover the written cells, with a margin so growth
//! amortizes, and is capped in area. A write that would push the window past
//! the cap leaves the cell out and sets `spill`; readers then consult the hash
//! map for cells outside the window, so pathological coordinates degrade
//! speed, never values.

#[derive(Clone, Default)]
pub struct DenseMirror {
    x0: i32,
    y0: i32,
    w: i32,
    h: i32,
    data: Vec<i32>,
    /// Some nonzero cell lies outside the window: outside reads must fall
    /// back to the authoritative map.
    pub spill: bool,
}

/// Window area cap (cells). 2^25 cells = 128 MiB of i32; a 0.05 mm grid over
/// 280 x 280 mm fits.
const MAX_CELLS: i64 = 1 << 25;
const MARGIN: i32 = 64;

pub enum Probe {
    /// Value is exact.
    Hit(i32),
    /// Outside the window with spill set: ask the map.
    Miss,
}

impl DenseMirror {
    #[inline]
    fn index(&self, gx: i32, gy: i32) -> Option<usize> {
        let dx = gx.wrapping_sub(self.x0);
        let dy = gy.wrapping_sub(self.y0);
        if (dx as u32) < (self.w as u32) && (dy as u32) < (self.h as u32) {
            Some(dy as usize * self.w as usize + dx as usize)
        } else {
            None
        }
    }

    #[inline]
    pub fn probe(&self, gx: i32, gy: i32) -> Probe {
        match self.index(gx, gy) {
            // SAFETY of the index: index() bounds-checks against w, h and
            // data.len() == w * h.
            Some(i) => Probe::Hit(self.data[i]),
            None if self.spill => Probe::Miss,
            None => Probe::Hit(0),
        }
    }

    /// Store `value` for the cell (the map's value after the write).
    #[inline]
    pub fn set(&mut self, gx: i32, gy: i32, value: i32) {
        if let Some(i) = self.index(gx, gy) {
            self.data[i] = value;
            return;
        }
        if value == 0 {
            return;
        }
        if self.grow_to(gx, gy) {
            let i = self.index(gx, gy).expect("grown window covers the cell");
            self.data[i] = value;
        } else {
            self.spill = true;
        }
    }

    /// Zero every cell; keeps the window allocated (the next stamp sequence
    /// lands in the same area).
    pub fn clear(&mut self) {
        self.data.iter_mut().for_each(|v| *v = 0);
        self.spill = false;
    }

    fn grow_to(&mut self, gx: i32, gy: i32) -> bool {
        let (nx0, ny0, nx1, ny1) = if self.data.is_empty() {
            (gx as i64 - MARGIN as i64, gy as i64 - MARGIN as i64,
             gx as i64 + MARGIN as i64, gy as i64 + MARGIN as i64)
        } else {
            let (x0, y0) = (self.x0 as i64, self.y0 as i64);
            let (x1, y1) = (x0 + self.w as i64 - 1, y0 + self.h as i64 - 1);
            // Grow by at least half the current size on the side that needs it.
            let mx = (self.w as i64 / 2).max(MARGIN as i64);
            let my = (self.h as i64 / 2).max(MARGIN as i64);
            (
                if (gx as i64) < x0 { gx as i64 - mx } else { x0 },
                if (gy as i64) < y0 { gy as i64 - my } else { y0 },
                if (gx as i64) > x1 { gx as i64 + mx } else { x1 },
                if (gy as i64) > y1 { gy as i64 + my } else { y1 },
            )
        };
        let (nx0, ny0) = (nx0.max(i32::MIN as i64 / 2), ny0.max(i32::MIN as i64 / 2));
        let (nx1, ny1) = (nx1.min(i32::MAX as i64 / 2), ny1.min(i32::MAX as i64 / 2));
        let nw = nx1 - nx0 + 1;
        let nh = ny1 - ny0 + 1;
        if nw <= 0 || nh <= 0 || nw * nh > MAX_CELLS
            || (gx as i64) < nx0 || (gx as i64) > nx1 || (gy as i64) < ny0 || (gy as i64) > ny1 {
            return false;
        }
        let mut data = vec![0i32; (nw * nh) as usize];
        if !self.data.is_empty() {
            let ox = (self.x0 as i64 - nx0) as usize;
            let oy = (self.y0 as i64 - ny0) as usize;
            let (w, nwu) = (self.w as usize, nw as usize);
            for row in 0..self.h as usize {
                let src = &self.data[row * w..row * w + w];
                let d = (oy + row) * nwu + ox;
                data[d..d + w].copy_from_slice(src);
            }
        }
        self.x0 = nx0 as i32;
        self.y0 = ny0 as i32;
        self.w = nw as i32;
        self.h = nh as i32;
        self.data = data;
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rustc_hash::FxHashMap;

    fn lcg(s: &mut u64) -> u64 {
        *s = s.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        *s >> 33
    }

    fn read(d: &DenseMirror, m: &FxHashMap<(i32, i32), i32>, x: i32, y: i32) -> i32 {
        match d.probe(x, y) {
            Probe::Hit(v) => v,
            Probe::Miss => m.get(&(x, y)).copied().unwrap_or(0),
        }
    }

    #[test]
    fn mirrors_map_with_growth_clear_and_spill() {
        let mut d = DenseMirror::default();
        let mut m: FxHashMap<(i32, i32), i32> = FxHashMap::default();
        let mut s = 11u64;
        for step in 0..100_000 {
            let (x, y) = if step % 997 == 0 {
                // far outlier: beyond the cap, must spill and still read right
                (1_000_000_000 - (lcg(&mut s) % 5) as i32, -1_000_000_000)
            } else {
                ((lcg(&mut s) % 900) as i32 - 450, (lcg(&mut s) % 700) as i32 - 200)
            };
            let v = (lcg(&mut s) % 50) as i32 + 1;
            let e = m.entry((x, y)).or_insert(0);
            *e = (*e).max(v);
            d.set(x, y, *e);
            if step % 40_000 == 39_999 {
                m.clear();
                d.clear();
            }
        }
        assert!(d.spill);
        for x in -500..500 {
            for y in -260..560 {
                assert_eq!(read(&d, &m, x, y), m.get(&(x, y)).copied().unwrap_or(0));
            }
        }
        for (&(x, y), &v) in &m {
            assert_eq!(read(&d, &m, x, y), v);
        }
    }
}
