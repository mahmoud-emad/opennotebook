//! The `mindmap` domain: a map of the topics a collection's sources cover.
//!
//! The generation is `opennotebook_script::mindmap`; this file reads the
//! collection's sources, hands them over, and keeps what comes back. See
//! `docs/mindmap-spec.md`.
//!
//! # Where maps live
//!
//! `var/opennotebook/mindmaps/<cid>/<id>.json`, one file per map, in the store
//! `filestore` keeps for maps and notes alike. The wire field is still named
//! `sid`: it is the collection's cid, which is the staging id its sources were
//! always added under.

use std::path::PathBuf;
use std::time::Duration;

use async_trait::async_trait;

use crate::create;
use crate::filestore;
use crate::mindmap::{
    MindMap, MindMapServiceApi, MindMapSummary, MindNode, MindmapCreateInput, MindmapCreateOutput,
    MindmapDeleteInput, MindmapDeleteOutput, MindmapEstimateInput, MindmapEstimateOutput,
    MindmapGetInput, MindmapGetOutput, MindmapListAllInput, MindmapListAllOutput, MindmapListInput,
    MindmapListOutput, MindmapRetitleInput, MindmapRetitleOutput,
};
use opennotebook_script::mindmap as mm;

pub struct MindMapService;

type Ctx = opennotebook_api::RequestContext;
type RpcError = opennotebook_api::RpcError;

/// How long a map may take. One call over at most about 150k tokens; a minute
/// is far past what that needs and short of a person giving up on the page.
const CREATE_TIMEOUT: Duration = Duration::from_secs(60);

#[async_trait]
impl MindMapServiceApi for MindMapService {
    async fn mindmap_create(
        &self,
        _ctx: &Ctx,
        input: MindmapCreateInput,
    ) -> Result<MindmapCreateOutput, RpcError> {
        let req = input.req;
        let sid = create::safe_sid(&req.sid).map_err(bad_request)?.to_string();
        let docs = crate::sources_impl::read_docs(&sid, req.sources.as_deref(), "map")?;

        let collection_title = collection_title(&sid).await;
        // The root's name when the model leaves it out: the one source's own
        // title, else the collection's, else a plain word for several.
        let hint = if docs.len() == 1 {
            docs[0].title.clone()
        } else {
            collection_title
                .clone()
                .unwrap_or_else(|| GENERIC_ROOT.to_string())
        };
        let focus = req
            .focus
            .as_deref()
            .map(str::trim)
            .filter(|f| !f.is_empty())
            .map(str::to_string);

        let made =
            tokio::time::timeout(CREATE_TIMEOUT, mm::generate(&docs, &hint, focus.as_deref()))
                .await
                .map_err(|_| {
                    internal(format!(
                        "the model took longer than {}s to map the sources",
                        CREATE_TIMEOUT.as_secs()
                    ))
                })?
                .map_err(|e| internal(format!("the mind map could not be made: {e}")))?;

        let created_ms = crate::collection::now_ms();
        let map = MindMap {
            id: uuid::Uuid::new_v4().to_string(),
            sid: sid.clone(),
            title: map_title(&made.root.name, collection_title.as_deref()),
            focus: focus.unwrap_or_default(),
            sources: docs.iter().map(|d| d.name.clone()).collect(),
            excerpted: made.excerpted,
            model: made.model,
            created_ms: created_ms as _,
            node_count: made.root.count() as _,
            dropped: made.dropped as _,
            unchecked: made.unchecked,
            root: to_wire(&made.root),
        };
        // Kept only while the collection is: a minute of model time is long
        // enough for somebody to delete it, and a map saved after that would
        // bring it back.
        let kept =
            crate::collection::output_saved(&sid, async { filestore::save(&maps_root(), &map) })
                .await
                .map_err(internal)?;
        if kept.is_none() {
            return Err(bad_request(format!(
                "collection `{sid}` was deleted while the map was made; nothing was kept"
            )));
        }
        flat(&map)
    }

    async fn mindmap_estimate(
        &self,
        _ctx: &Ctx,
        input: MindmapEstimateInput,
    ) -> Result<MindmapEstimateOutput, RpcError> {
        let docs = crate::sources_impl::read_docs(&input.sid, None, "map")?;
        let chars: u64 = docs.iter().map(|d| d.text.chars().count() as u64).sum();
        let (input_tokens, output_tokens) = tokens_for(chars);
        let model = opennotebook_session::settings::mindmap_model().await;
        let price = crate::estimate_live::price_of(&model).await;
        let one = price.map_or(0.0, |(p_in, p_out)| {
            input_tokens as f64 * p_in + output_tokens as f64 * p_out
        });
        Ok(MindmapEstimateOutput {
            sources: docs.len() as _,
            chars: chars as _,
            model,
            input_tokens: input_tokens as _,
            output_tokens: output_tokens as _,
            cost_usd: one,
            cost_high_usd: 2.0 * one,
            priced: price.is_some(),
        })
    }

    async fn mindmap_list(
        &self,
        _ctx: &Ctx,
        input: MindmapListInput,
    ) -> Result<MindmapListOutput, RpcError> {
        let sid = create::safe_sid(&input.sid).map_err(bad_request)?;
        let maps = filestore::list(&maps_root(), sid)
            .into_iter()
            .map(summary)
            .collect();
        Ok(MindmapListOutput { maps })
    }

    async fn mindmap_list_all(
        &self,
        _ctx: &Ctx,
        _input: MindmapListAllInput,
    ) -> Result<MindmapListAllOutput, RpcError> {
        let maps = filestore::list_all(&maps_root())
            .into_iter()
            .map(summary)
            .collect();
        Ok(MindmapListAllOutput { maps })
    }

    async fn mindmap_get(
        &self,
        _ctx: &Ctx,
        input: MindmapGetInput,
    ) -> Result<MindmapGetOutput, RpcError> {
        let req = input.req;
        let sid = create::safe_sid(&req.sid).map_err(bad_request)?;
        let id = safe_id(&req.id).map_err(bad_request)?;
        match filestore::load::<MindMap>(&maps_root(), sid, id) {
            Some(map) => flat(&map),
            None => Err(bad_request(format!(
                "collection `{sid}` has no mind map `{id}`"
            ))),
        }
    }

    async fn mindmap_delete(
        &self,
        _ctx: &Ctx,
        input: MindmapDeleteInput,
    ) -> Result<MindmapDeleteOutput, RpcError> {
        let req = input.req;
        let sid = create::safe_sid(&req.sid).map_err(bad_request)?;
        let id = safe_id(&req.id).map_err(bad_request)?;
        let removed =
            filestore::remove(&filestore::path(&maps_root(), sid, id)).map_err(internal)?;
        Ok(MindmapDeleteOutput { value: removed })
    }

    /// Rename a map. The title only: the root keeps the name the map was
    /// drawn with, because the tree is what the sources said and the title
    /// is what the person calls it. Empty is refused for the reason
    /// `session_retitle` gives: it is what an untouched input sends, and a
    /// map with no title has nothing to be listed by.
    async fn mindmap_retitle(
        &self,
        _ctx: &Ctx,
        input: MindmapRetitleInput,
    ) -> Result<MindmapRetitleOutput, RpcError> {
        let req = input.req;
        let sid = create::safe_sid(&req.sid).map_err(bad_request)?;
        let id = safe_id(&req.id).map_err(bad_request)?;
        let title = req.title.trim();
        if title.is_empty() {
            return Err(bad_request(
                "a mind map title cannot be empty: nothing in the list would name it",
            ));
        }
        let renamed =
            filestore::retitle(&filestore::path(&maps_root(), sid, id), title).map_err(internal)?;
        if renamed {
            // A rename is a change to the collection, which lists by it.
            crate::collection::touched(sid).await;
        }
        Ok(MindmapRetitleOutput { value: renamed })
    }
}

/// A map as the list shows it.
fn summary(m: MindMap) -> MindMapSummary {
    MindMapSummary {
        id: m.id,
        sid: m.sid,
        title: m.title,
        focus: m.focus,
        node_count: m.node_count,
        created_ms: m.created_ms,
        shape: m
            .root
            .children
            .iter()
            .map(|c| c.children.len() as _)
            .collect(),
        sources: m.sources,
    }
}

/// A map as a method's output. The macro flattens a returned struct's fields
/// into the output type, so `MindmapCreateOutput` and `MindmapGetOutput` are
/// each `MindMap` field for field under another name. Going through the JSON
/// both already serialise to keeps one conversion instead of two hand copies
/// that would drift the day a field is added.
fn flat<T: serde::de::DeserializeOwned>(map: &MindMap) -> Result<T, RpcError> {
    serde_json::to_value(map)
        .and_then(serde_json::from_value)
        .map_err(|e| internal(format!("mind map output: {e}")))
}

/// Tokens in and out of one map call for `chars` of source text.
///
/// In: the text at four characters a token, capped where the generator
/// switches to excerpts, plus about 500 for the prompt and the source headers.
/// Out: 1,000, above what the measured maps used (20 to 75 nodes of a few words
/// each, two spaces of indent a level), so the estimate does not undershoot.
fn tokens_for(chars: u64) -> (u64, u64) {
    let sent = chars.min(mm::WHOLE_TEXT_CHARS as u64);
    (sent.div_ceil(4) + 500, 1_000)
}

fn to_wire(n: &mm::MindNode) -> MindNode {
    MindNode {
        name: n.name.clone(),
        children: n.children.iter().map(to_wire).collect(),
    }
}

// ── the store ───────────────────────────────────────────────────────────────

pub(crate) fn maps_root() -> PathBuf {
    opennotebook_session::paths::data_dir().join("mindmaps")
}

/// A map id is a UUID, or `m` and digits for a map made before UUIDs.
/// Anything else is refused before it reaches a path, so an id can never walk
/// out of its sid's directory.
fn safe_id(id: &str) -> Result<&str, String> {
    let legacy = id.len() > 1
        && id.len() <= 32
        && id.starts_with('m')
        && id[1..].chars().all(|c| c.is_ascii_digit());
    let uuid = uuid::Uuid::try_parse(id).is_ok_and(|u| u.hyphenated().to_string() == id);
    if legacy || uuid {
        Ok(id)
    } else {
        Err(format!("`{id}` is not a mind map id"))
    }
}

fn internal(e: impl std::fmt::Display) -> RpcError {
    RpcError::internal(e.to_string())
}

fn bad_request(e: impl std::fmt::Display) -> RpcError {
    RpcError::invalid_params(e.to_string())
}

/// The root's name when nothing better is known; never a map's title when its
/// collection has one.
const GENERIC_ROOT: &str = "Your sources";

/// The collection's title, when it has one yet.
async fn collection_title(cid: &str) -> Option<String> {
    let store = crate::collection::open_store().await.ok()?;
    store
        .collections()
        .get(cid)
        .await
        .ok()
        .flatten()
        .map(|c| create::one_line(&c.title))
        .filter(|t| !t.is_empty())
}

/// A map is titled by its root topic, unless that is the generic "Your
/// sources", which would name every map of every collection the same.
fn map_title(root: &str, collection: Option<&str>) -> String {
    let root = root.trim();
    if root.is_empty() || root.eq_ignore_ascii_case(GENERIC_ROOT) {
        collection.unwrap_or(GENERIC_ROOT).to_string()
    } else {
        root.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_root(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("hs_mm_{tag}_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    fn sample(id: &str, created_ms: u64) -> MindMap {
        MindMap {
            id: id.into(),
            sid: "s1".into(),
            title: "Rust".into(),
            focus: String::new(),
            sources: vec!["a.md".into()],
            excerpted: false,
            model: "m".into(),
            created_ms: created_ms as _,
            node_count: 3,
            dropped: 0,
            unchecked: false,
            root: MindNode {
                name: "Rust".into(),
                children: vec![MindNode {
                    name: "Ownership".into(),
                    children: vec![MindNode {
                        name: "Borrowing".into(),
                        children: vec![],
                    }],
                }],
            },
        }
    }

    /// The build ingests every file in a collection's staging directory, so a map
    /// stored anywhere under it would become a source of the session.
    #[test]
    fn a_map_is_never_stored_where_the_build_reads_sources() {
        let staging = create::staging_dir("s1790947153376");
        let maps = maps_root().join("s1790947153376");
        assert!(
            !maps.starts_with(&staging),
            "{maps:?} is inside {staging:?}"
        );
        assert!(!staging.starts_with(&maps));
    }

    #[test]
    fn a_saved_map_reads_back_whole_tree_included() {
        let root = temp_root("roundtrip");
        let m = sample("m100", 100);
        filestore::save(&root, &m).unwrap();
        let back = filestore::load::<MindMap>(&root, "s1", "m100").unwrap();
        assert_eq!(back.root.children[0].children[0].name, "Borrowing");
        assert_eq!(
            serde_json::to_value(&back).unwrap(),
            serde_json::to_value(&m).unwrap()
        );
        // No temporary file is left behind.
        let names: Vec<_> = std::fs::read_dir(root.join("s1"))
            .unwrap()
            .flatten()
            .map(|e| e.file_name())
            .collect();
        assert_eq!(names.len(), 1);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn maps_list_newest_first_and_a_broken_file_hides_nothing() {
        let root = temp_root("list");
        filestore::save(&root, &sample("m100", 100)).unwrap();
        filestore::save(&root, &sample("m300", 300)).unwrap();
        filestore::save(&root, &sample("m200", 200)).unwrap();
        std::fs::write(root.join("s1/m999.json"), b"{ not json").unwrap();
        let ids: Vec<String> = filestore::list::<MindMap>(&root, "s1")
            .into_iter()
            .map(|m| m.id)
            .collect();
        assert_eq!(ids, ["m300", "m200", "m100"]);
        assert!(filestore::list::<MindMap>(&root, "nobody").is_empty());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn every_map_of_every_collection_lists_together_newest_first() {
        let root = temp_root("all");
        filestore::save(&root, &sample("m100", 100)).unwrap();
        let mut other = sample("m300", 300);
        other.sid = "s2".into();
        filestore::save(&root, &other).unwrap();
        filestore::save(&root, &sample("m200", 200)).unwrap();
        let got: Vec<(String, String)> = filestore::list_all::<MindMap>(&root)
            .into_iter()
            .map(|m| (m.sid, m.id))
            .collect();
        assert_eq!(
            got,
            [
                ("s2".into(), "m300".into()),
                ("s1".into(), "m200".into()),
                ("s1".into(), "m100".into())
            ]
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn an_id_cannot_leave_its_directory() {
        assert!(safe_id("m1790947153376").is_ok());
        assert!(safe_id("6f1c2a3e-8b4d-4c1f-9a2e-0d3b5c7e9f11").is_ok());
        for bad in [
            "",
            "m",
            "../m1",
            "m1/../../x",
            "x123",
            "m12a",
            "m1.json",
            // A UUID in any spelling but the canonical one is refused too, so
            // one map has one file name.
            "6F1C2A3E-8B4D-4C1F-9A2E-0D3B5C7E9F11",
            "6f1c2a3e8b4d4c1f9a2e0d3b5c7e9f11",
            "{6f1c2a3e-8b4d-4c1f-9a2e-0d3b5c7e9f11}",
            "../6f1c2a3e-8b4d-4c1f-9a2e-0d3b5c7e9f11",
        ] {
            assert!(safe_id(bad).is_err(), "{bad}");
        }
    }

    /// Both outputs are the map field for field; a field added to `MindMap`
    /// and not to them, or the other way round, fails here.
    #[test]
    fn a_summary_carries_the_outline_of_its_tree() {
        let s = summary(sample("m100", 100));
        assert_eq!(s.sid, "s1");
        assert_eq!(s.shape, vec![1]);
    }

    #[test]
    fn every_new_map_gets_its_own_uuid() {
        let a = uuid::Uuid::new_v4().to_string();
        let b = uuid::Uuid::new_v4().to_string();
        assert_ne!(a, b);
        assert!(safe_id(&a).is_ok(), "{a}");
    }

    #[test]
    fn a_generic_root_does_not_title_the_map() {
        assert_eq!(
            map_title("Photosynthesis", Some("Leaves")),
            "Photosynthesis"
        );
        assert_eq!(map_title("Your sources", Some("Leaves")), "Leaves");
        assert_eq!(map_title("your sources ", None), "Your sources");
        assert_eq!(map_title("", Some("Leaves")), "Leaves");
    }

    #[test]
    fn a_rename_changes_the_title_and_nothing_else() {
        let root = temp_root("retitle");
        let m = sample("m100", 100);
        filestore::save(&root, &m).unwrap();
        // A field a newer build wrote, which this build's type does not know.
        let path = filestore::path(&root, "s1", "m100");
        let mut v: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        v["layout"] = serde_json::json!("radial");
        std::fs::write(&path, serde_json::to_vec(&v).unwrap()).unwrap();

        assert!(filestore::retitle(&path, "Ownership in Rust").unwrap());
        let back = filestore::load::<MindMap>(&root, "s1", "m100").unwrap();
        assert_eq!(back.title, "Ownership in Rust");
        assert_eq!(
            back.root.name, "Rust",
            "the tree keeps the name it was drawn with"
        );
        assert_eq!(back.root.children[0].children[0].name, "Borrowing");
        let raw: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(
            raw["layout"], "radial",
            "an unknown field survives a rename"
        );
        assert!(
            !path.with_extension("json.tmp").exists(),
            "a temporary file was left behind"
        );

        // No such map is `false`, not an error, and writes nothing.
        let missing = filestore::path(&root, "s1", "m999");
        assert!(!filestore::retitle(&missing, "x").unwrap());
        assert!(!missing.exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn a_map_flattens_into_both_outputs() {
        let m = sample("m100", 100);
        let got: crate::mindmap::MindmapGetOutput = flat(&m).unwrap();
        assert_eq!(got.root.children[0].name, "Ownership");
        let made: crate::mindmap::MindmapCreateOutput = flat(&m).unwrap();
        assert_eq!(made.id, "m100");
        assert_eq!(
            serde_json::to_value(&got).unwrap(),
            serde_json::to_value(&m).unwrap()
        );
    }

    #[test]
    fn the_estimate_counts_the_text_sent_not_the_text_staged() {
        // The 189,870 byte collection: about 47.5k tokens of text and the prompt.
        assert_eq!(tokens_for(189_870), (47_468 + 500, 1_000));
        // Past the cap the generator sends excerpts, so the estimate stops growing.
        let cap = tokens_for(mm::WHOLE_TEXT_CHARS as u64);
        assert_eq!(tokens_for(5_000_000), cap);
        assert_eq!(tokens_for(0), (500, 1_000));
    }

    #[test]
    fn the_wire_tree_matches_the_generated_one() {
        let g =
            mm::parse_outline("- Rust\n  - Ownership\n    - Borrowing\n  - Traits\n", "x").unwrap();
        let w = to_wire(&g);
        assert_eq!(w.children.len(), 2);
        assert_eq!(w.children[0].children[0].name, "Borrowing");
        assert!(w.children[1].children.is_empty());
    }
}
