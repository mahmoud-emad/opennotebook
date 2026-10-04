//! Raw `extern "C"` surface for the AudioWorklet. No wasm-bindgen.
//!
//! An `AudioWorkletGlobalScope` has no DOM, no `fetch`, no `URL` and no module
//! loader, which is most of what bindgen's generated glue reaches for. The
//! worklet instead receives the module bytes over its port, calls
//! `WebAssembly.instantiate`, and talks to these six functions through the
//! instance's exported linear memory.
//!
//! The contract, which `vad-worklet.js` implements:
//!
//! ```text
//! const { instance } = await WebAssembly.instantiate(bytes, {});
//! const ex  = instance.exports;
//! const vad = ex.vad_new(sampleRate);
//! const buf = ex.vad_alloc(128);                 // render quantum
//! const mem = () => new Float32Array(ex.memory.buffer, buf, 128);
//! // per render quantum:
//! mem().set(input);
//! const ev = ex.vad_push(vad, buf, 128);         // 0 idle, 1 speaking, 2 ended
//! ```
//!
//! `ex.memory.buffer` is re-read on every use on purpose: growing the heap
//! detaches the old `ArrayBuffer` and a cached view silently writes nowhere.
//!
//! Every pointer crossing this boundary came from [`vad_new`] or [`vad_alloc`].
//! Nothing here validates that, because there is nothing useful to do about it
//! in a wasm module with no way to report; the JS side owns the discipline.

use alloc_shim::*;

use crate::{Config, Event, Turn};

mod alloc_shim {
    pub use std::alloc::{Layout, alloc, dealloc};
}

/// [`Event`] as it crosses the boundary.
const EV_IDLE: u32 = 0;
const EV_SPEAKING: u32 = 1;
const EV_ENDED: u32 = 2;

fn code(e: Event) -> u32 {
    match e {
        Event::Idle => EV_IDLE,
        Event::Speaking => EV_SPEAKING,
        Event::Ended => EV_ENDED,
    }
}

/// Construct a detector for a capture stream running at `sample_rate`.
///
/// Thresholds are [`Config::DEFAULT`]. They are deliberately not parameters:
/// the point of this crate is that the page cannot hold a threshold the replay
/// harness does not. Changing one is a change here, measured first.
#[unsafe(no_mangle)]
pub extern "C" fn vad_new(sample_rate: f32) -> *mut Turn {
    Box::into_raw(Box::new(Turn::new(sample_rate, Config::DEFAULT)))
}

/// Release a detector from [`vad_new`].
///
/// # Safety
/// `ptr` must have come from [`vad_new`] and must not be used afterwards.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vad_free(ptr: *mut Turn) {
    if !ptr.is_null() {
        drop(unsafe { Box::from_raw(ptr) });
    }
}

/// Put one buffer of mono `f32` samples through the detector.
///
/// Returns 0 idle, 1 speaking, 2 ended. Once it returns 2 it keeps returning 2
/// until [`vad_reset`].
///
/// # Safety
/// `samples` must point at `len` readable `f32`s, which is what [`vad_alloc`]
/// hands out.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vad_push(ptr: *mut Turn, samples: *const f32, len: usize) -> u32 {
    let Some(turn) = (unsafe { ptr.as_mut() }) else {
        return EV_IDLE;
    };
    if samples.is_null() || len == 0 {
        return code(turn.push(&[]));
    }
    code(turn.push(unsafe { core::slice::from_raw_parts(samples, len) }))
}

/// Milliseconds of speech this turn has seen. The same quantity the server
/// reports as `speech_ms`, so the page and the log can be compared.
///
/// # Safety
/// `ptr` must have come from [`vad_new`].
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vad_speech_ms(ptr: *mut Turn) -> u32 {
    unsafe { ptr.as_ref() }.map_or(0, Turn::speech_ms)
}

/// Back to a fresh turn without reallocating.
///
/// # Safety
/// `ptr` must have come from [`vad_new`].
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vad_reset(ptr: *mut Turn) {
    if let Some(t) = unsafe { ptr.as_mut() } {
        t.reset();
    }
}

/// A scratch buffer of `len` `f32`s for the worklet to write samples into.
///
/// Allocated once at construction and reused, never per render quantum: a
/// worklet's `process` runs on the audio thread every 2.7 ms and allocation
/// there is how a page starts to crackle.
#[unsafe(no_mangle)]
pub extern "C" fn vad_alloc(len: usize) -> *mut f32 {
    if len == 0 {
        return core::ptr::null_mut();
    }
    match Layout::array::<f32>(len) {
        Ok(l) => unsafe { alloc(l) }.cast::<f32>(),
        Err(_) => core::ptr::null_mut(),
    }
}

/// Release a buffer from [`vad_alloc`].
///
/// # Safety
/// `ptr` and `len` must be exactly what [`vad_alloc`] returned and was given.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vad_dealloc(ptr: *mut f32, len: usize) {
    if ptr.is_null() || len == 0 {
        return;
    }
    if let Ok(l) = Layout::array::<f32>(len) {
        unsafe { dealloc(ptr.cast::<u8>(), l) };
    }
}
