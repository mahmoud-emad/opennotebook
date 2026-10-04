//! The `session` domain.
//!
//! The interesting one is `session_prepare`, which is where the prep job stops
//! being a record and becomes a child process the server spawns. Beside it sit the
//! gallery's own edits — delete, retitle, pin — which touch only the session row.

use async_trait::async_trait;

use opennotebook_session::{DeckRef as StoreDeckRef, SessionState, SessionStore, SlideRef};

use crate::playback::{self, PlayState, Playhead as Head};
use crate::session::{
    Aspect, CollectionCoverRefreshInput, CollectionCoverRefreshOutput, CollectionCreateInput,
    CollectionCreateOutput, CollectionDeleteInput, CollectionDeleteOutput, CollectionGetInput,
    CollectionGetOutput, CollectionListInput, CollectionListOutput, CollectionPinInput,
    CollectionPinOutput, CollectionRetitleInput, CollectionRetitleOutput, Cue, DeckRef,
    NarrationLine, PlaybackAtReq, Session, SessionAskInput, SessionAskOutput, SessionBuildInput,
    SessionBuildOutput, SessionDeleteInput, SessionDeleteOutput, SessionEstimateInput,
    SessionEstimateOutput, SessionGetInput, SessionGetOutput, SessionListInput, SessionListOutput,
    SessionPinInput, SessionPinOutput, SessionPrepareInput, SessionPrepareOutput,
    SessionRetitleInput, SessionRetitleOutput, SessionServiceApi, SessionSlide, SessionSummary,
    Speaker,
};
use crate::{AUDIO_ROUTE, SLIDE_ROUTE};

pub struct SessionService;

#[async_trait]
impl SessionServiceApi for SessionService {
    /// Start a collection. Its cid is minted like a sid (`s<epoch ms>`) and its
    /// staging directory is made at once, so the sources tools accept it
    /// straight away.
    async fn collection_create(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: CollectionCreateInput,
    ) -> Result<CollectionCreateOutput, opennotebook_api::RpcError> {
        let title = input.req.title.unwrap_or_default();
        let c = crate::collection::create(&title).await.map_err(internal)?;
        Ok(CollectionCreateOutput {
            cid: c.cid,
            title: c.title,
            title_auto: c.title_auto,
            created_ms: c.created_ms as i64,
            updated_ms: c.updated_ms as i64,
            pinned: c.pinned,
        })
    }

    async fn collection_list(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        _input: CollectionListInput,
    ) -> Result<CollectionListOutput, opennotebook_api::RpcError> {
        let store = store().await?;
        let g = crate::collection::gather(&store).await.map_err(internal)?;
        let collections = crate::collection::synthesize(&g);
        // A collection whose cover is older than what it holds — a legacy one,
        // or one an output finished in while nothing was listening — gets one
        // designed in the background. This list shows the cover it has.
        crate::collection::enqueue_covers(crate::collection::stale_covers(&g, &collections));
        Ok(CollectionListOutput { collections })
    }

    async fn collection_get(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: CollectionGetInput,
    ) -> Result<CollectionGetOutput, opennotebook_api::RpcError> {
        let cid = crate::create::safe_sid(&input.cid).map_err(bad_request)?;
        let store = store().await?;
        // The list's own fold over this collection's things alone: a
        // collection's summary is the same whichever call shows it, and the
        // list is the one place that says what that is.
        let g = crate::collection::gather_one(&store, cid)
            .await
            .map_err(internal)?;
        let collection = crate::collection::synthesize(&g)
            .into_iter()
            .find(|c| c.cid == cid);
        // As the list does: a cover older than what the collection now holds
        // (an output finished since it was drawn) is designed again in the
        // background, so opening the collection is enough to bring it current.
        if let Some(c) = &collection {
            crate::collection::enqueue_covers(crate::collection::stale_covers(
                &g,
                std::slice::from_ref(c),
            ));
        }
        Ok(CollectionGetOutput {
            found: collection.is_some(),
            outputs: crate::collection::outputs_of(cid, g.sessions),
            collection,
        })
    }

    /// Rename a collection. An empty title hands the naming back to the
    /// studio: the title is cleared and named again from the sources, in the
    /// background, so a caller polling `collection_get` sees it arrive.
    async fn collection_retitle(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: CollectionRetitleInput,
    ) -> Result<CollectionRetitleOutput, opennotebook_api::RpcError> {
        let req = input.req;
        let cid = crate::create::safe_sid(&req.cid).map_err(bad_request)?;
        let store = store().await?;
        let value = crate::collection::retitle(&store, cid, &req.title)
            .await
            .map_err(internal)?;
        Ok(CollectionRetitleOutput { value })
    }

    async fn collection_pin(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: CollectionPinInput,
    ) -> Result<CollectionPinOutput, opennotebook_api::RpcError> {
        let req = input.req;
        let cid = crate::create::safe_sid(&req.cid).map_err(bad_request)?;
        let store = store().await?;
        let value = crate::collection::pin(&store, cid, req.pinned)
            .await
            .map_err(internal)?;
        Ok(CollectionPinOutput { value })
    }

    /// Design a collection's cover again now, whatever changed or did not.
    async fn collection_cover_refresh(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: CollectionCoverRefreshInput,
    ) -> Result<CollectionCoverRefreshOutput, opennotebook_api::RpcError> {
        let cid = crate::create::safe_sid(&input.cid).map_err(bad_request)?;
        let store = store().await?;
        let value = crate::collection::cover_refresh(&store, cid)
            .await
            .map_err(internal)?;
        Ok(CollectionCoverRefreshOutput { value })
    }

    /// Delete a collection: its sources, maps and notes, its decks and audio
    /// overviews as `session_delete` takes one, and its own row. See
    /// `collection::delete`.
    async fn collection_delete(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: CollectionDeleteInput,
    ) -> Result<CollectionDeleteOutput, opennotebook_api::RpcError> {
        let cid = crate::create::safe_sid(&input.cid).map_err(bad_request)?;
        let store = store().await?;
        let removed = crate::collection::delete(&store, cid)
            .await
            .map_err(internal)?;
        Ok(CollectionDeleteOutput { value: removed })
    }

    async fn session_get(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: SessionGetInput,
    ) -> Result<SessionGetOutput, opennotebook_api::RpcError> {
        let store = store().await?;
        let found = store.get(&input.sid).await.map_err(internal)?;
        let found = match found {
            Some(s) => Some(reconcile(&store, s).await),
            None => None,
        };

        // `found` is a field, not something a caller infers from `session`
        // being absent: absence is a missing row, and a caller should not have
        // to tell it from an empty value.
        Ok(match found {
            Some(s) => SessionGetOutput {
                found: true,
                session: Some(wire(s)),
            },
            None => SessionGetOutput {
                found: false,
                session: None,
            },
        })
    }

    async fn session_list(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        _input: SessionListInput,
    ) -> Result<SessionListOutput, opennotebook_api::RpcError> {
        let store = store().await?;
        let mut rows = Vec::new();
        for s in store.list().await.map_err(internal)? {
            rows.push(reconcile(&store, s).await);
        }
        let sessions = rows.into_iter().map(summary).collect();
        Ok(SessionListOutput { sessions })
    }

    /// Submit a prep run and return as soon as the job row exists.
    ///
    /// This does NOT wait for the session to prepare. A 20-slide deck is minutes
    /// of model calls and synthesis, so the contract is a job sid to poll,
    /// exactly as §3 specifies. `accepted: true` means the row was created; the
    /// outcome is `status` on the job row and nothing here.
    async fn session_prepare(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: SessionPrepareInput,
    ) -> Result<SessionPrepareOutput, opennotebook_api::RpcError> {
        let req = input.req;
        // The sid names the session directory AND the memory collection.
        // Two sessions colliding on a sid do not get two collections, they get
        // one collection written twice with the second import silently adopting
        // the first one's documents — so this is refused here rather than
        // discovered later.
        if !legal_sid(&req.sid) {
            return Err(bad_request(format!(
                "sid `{}` is not a legal single path segment and collection name: \
                 it must be non-empty, ASCII alphanumeric with `-` or `_`, and not start with a dot",
                req.sid
            )));
        }

        // An empty `resource_dir` means "use what was staged for this sid".
        //
        // The field is a path on this box, which is right for a prep job the
        // server spawns and impossible for a browser, which has no filesystem
        // here. The create flow stages typed notes and fetched pages under
        // `<data dir>/staging/<sid>` and then asks for a prepare with no
        // directory; resolving it here means the job still receives a real path
        // and nothing downstream has to know there are two ways in.
        let mut req = req;
        // An empty collection is no collection: the sid is its own, as it was
        // before collections, and the row records nothing.
        req.collection = req
            .collection
            .take()
            .map(|c| c.trim().to_string())
            .filter(|c| !c.is_empty());
        if let Some(cid) = &req.collection {
            crate::create::safe_sid(cid).map_err(bad_request)?;
        }
        // Whose staged sources an empty `resource_dir` means: the collection's
        // when one is named, which is how many outputs share one set of
        // sources; otherwise the sid's own, as before.
        let source_id = req.collection.clone().unwrap_or_else(|| req.sid.clone());
        if req.resource_dir.trim().is_empty() {
            let researching = req
                .research_topic
                .as_deref()
                .is_some_and(|t| !t.trim().is_empty());
            match crate::create::staged_path(&source_id) {
                Some(dir) => req.resource_dir = dir,
                // Nothing added, but the web will be read: research writes its
                // report into this same directory before ingest looks at it.
                None if researching => match crate::create::staging_dir_made(&source_id) {
                    Ok(dir) => req.resource_dir = dir,
                    Err(e) => return Err(internal(e)),
                },
                // Refused here rather than dispatched: a job that fails at
                // ingest leaves no session row, so the caller polling for one
                // waits forever on something that already gave up.
                None => {
                    return Err(bad_request(format!(
                        "no sources: nothing has been added to `{source_id}` and no resource_dir was given"
                    )));
                }
            }
        }

        // The settings fill what the request leaves out, by the same rule as
        // session_build, so a retry or an agent's bare prepare builds what the
        // dialog would.
        let sources = std::fs::read_dir(&req.resource_dir)
            .map(|rd| rd.flatten().filter(|e| e.path().is_file()).count())
            .unwrap_or(0);
        prep_defaults(&mut req, sources)
            .await
            .map_err(bad_request)?;
        req.title = output_title(
            &req.title,
            deck_style(&req).as_deref(),
            audio_spec(
                req.audio_format.as_deref(),
                req.audio_length.as_deref(),
                req.focus.as_deref(),
            )
            .as_ref(),
        );

        // Before anything is written or dispatched: a refused build leaves no
        // row and no job behind, only the reason.
        // Priced on the shape the pipeline will build, so an audio overview is
        // checked on its chapters and its format's voices.
        crate::estimate_live::check_limit(
            std::path::Path::new(&req.resource_dir),
            &prep_shape(&req),
            req.research_topic
                .as_deref()
                .is_some_and(|t| !t.trim().is_empty()),
        )
        .await
        .map_err(bad_request)?;

        let store = store().await?;
        if store.get(&req.sid).await.map_err(internal)?.is_some() {
            return Err(bad_request(format!(
                "session `{}` already exists; sids are unique per workspace",
                req.sid
            )));
        }

        // The row goes in BEFORE the job is submitted, as `Preparing`.
        //
        // The prep process writes the real row only once ingest and the script
        // are done — minutes in. Until then the session existed nowhere a page
        // could find it: a refresh, a restart of this service or a redeploy in
        // that window left a person with no gallery card and no progress for
        // work that was in fact running in its prep process. The job survives all
        // three; now the session does too.
        //
        // Before, not after: a prep that fails at once records a `Failed` row
        // (see `record_prep_failure`), and a placeholder written after the
        // submit could land on top of it and leave a dead job looking alive.
        let placeholder = opennotebook_session::Session {
            sid: req.sid.clone(),
            title: req.title.clone(),
            collection_name: req.sid.clone(),
            collection_sid: None,
            deck_ref: None,
            speakers: Vec::new(),
            slides: Vec::new(),
            state: SessionState::Preparing,
            prep_job_sid: None,
            failure: None,
            pinned: false,
            audio: audio_spec(
                req.audio_format.as_deref(),
                req.audio_length.as_deref(),
                req.focus.as_deref(),
            ),
            collection: req.collection.clone(),
            spent_usd: None,
            style: deck_style(&req),
        };
        // Into its collection under the collection's lock, and only while the
        // collection is there: a row naming a deleted one would list it again.
        match req.collection.as_deref() {
            Some(cid) => {
                if !crate::collection::output_added(&store, cid, &placeholder)
                    .await
                    .map_err(internal)?
                {
                    return Err(bad_request(format!(
                        "collection `{cid}` was deleted; nothing was built"
                    )));
                }
            }
            None => store.put(&placeholder).await.map_err(internal)?,
        }

        let job_sid = match crate::dispatch::submit(&req).await {
            Ok(j) => j,
            Err(e) => {
                // Nothing is running, so nothing may claim to be.
                let _ = store.delete(&req.sid).await;
                if let Some(cid) = req.collection.as_deref() {
                    let _ = crate::collection::output_removed(&store, cid, &req.sid).await;
                }
                return Err(internal(e));
            }
        };

        // Name the job on the row, so progress can be followed from the first
        // phase. Only onto a row that is still the untouched placeholder: if
        // the prep already failed and wrote why, that row stands.
        if let Ok(Some(mut row)) = store.get(&req.sid).await
            && row.state == SessionState::Preparing
            && row.prep_job_sid.is_none()
        {
            row.prep_job_sid = Some(job_sid.clone());
            let _ = store.put(&row).await;
        }

        Ok(SessionPrepareOutput {
            accepted: true,
            sid: req.sid,
            prep_job_sid: job_sid,
        })
    }

    /// Build from a collection's sources with the studio's defaults.
    ///
    /// The one build call an agent needs: it names a collection, and the title,
    /// speakers, voices, slide count and style come from the studio settings
    /// exactly as the collection page picks them — so an output built by an
    /// agent and one built in the browser come out the same.
    async fn session_build(
        &self,
        ctx: &opennotebook_api::RequestContext,
        input: SessionBuildInput,
    ) -> Result<SessionBuildOutput, opennotebook_api::RpcError> {
        let mut req = input.req;
        req.collection = req
            .collection
            .take()
            .map(|c| c.trim().to_string())
            .filter(|c| !c.is_empty());
        let BuildPlan {
            speakers,
            slide_count,
            style,
            audio,
            ..
        } = build_plan(&req).await?;
        let store = store().await?;
        // In a collection each output is its own session, so an empty sid is
        // the caller asking for a fresh one. Without a collection the sid IS
        // the collection, as before, and stays required.
        if wants_fresh_sid(&req) {
            req.sid = crate::collection::mint(&store).await.map_err(internal)?;
        }
        // Empty unless given: session_prepare names it by its kind.
        let title = req.title.clone().unwrap_or_default();
        let out = self
            .session_prepare(
                ctx,
                SessionPrepareInput {
                    req: crate::session::SessionPrepareReq {
                        sid: req.sid,
                        title,
                        // Empty: session_prepare resolves the collection's staging
                        // directory, which is where every source went.
                        resource_dir: String::new(),
                        speakers,
                        slide_count: Some(slide_count),
                        style: Some(style).filter(|s| !s.is_empty()),
                        research_topic: None,
                        audio_format: audio.as_ref().map(|a| a.format.id().to_string()),
                        audio_length: audio.as_ref().map(|a| a.length().id().to_string()),
                        focus: audio.map(|a| a.focus).filter(|f| !f.is_empty()),
                        collection: req.collection.clone(),
                    },
                },
            )
            .await?;
        Ok(SessionBuildOutput {
            accepted: out.accepted,
            sid: out.sid,
            prep_job_sid: out.prep_job_sid,
        })
    }

    /// What `session_build` would cost with the same arguments.
    ///
    /// Same request, same defaults (`build_plan`), so the estimate describes
    /// exactly the build the button next to it would start. No model is called:
    /// the sources are measured on disk, the prices come from the AI endpoint's
    /// catalog, and the slide model is the one in the studio's settings.
    async fn session_estimate(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: SessionEstimateInput,
    ) -> Result<SessionEstimateOutput, opennotebook_api::RpcError> {
        let plan = build_plan(&input.req).await?;
        crate::estimate_live::estimate(source_sid(&input.req), &plan)
            .await
            .map_err(internal)
    }

    /// Answer a question about a built session, in text.
    ///
    /// The spoken answers during playback stream audio from an audio model; an
    /// agent wants words. So this is the same grounding — the session's own
    /// memory collection, retrieved on the question — answered by the chat
    /// model, starting from the slide the listener was on when one is named.
    async fn session_ask(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: SessionAskInput,
    ) -> Result<SessionAskOutput, opennotebook_api::RpcError> {
        use opennotebook_ai::Message;

        let req = input.req;
        let question = req.question.trim();
        if question.is_empty() {
            return Err(bad_request("the question is empty".into()));
        }
        let store = store().await?;
        let Some(session) = store.get(&req.sid).await.map_err(internal)? else {
            return Err(bad_request(format!("no session `{}`", req.sid)));
        };
        if session.state != SessionState::Ready {
            return Err(bad_request(format!(
                "session `{}` is not ready yet; poll session_get",
                req.sid
            )));
        }
        let memory = opennotebook_memory::from_settings()
            .await
            .map_err(|e| internal(format!("the retrieval store: {e}")))?;
        let grounding = opennotebook_script::grounding::retrieve(
            &memory,
            crate::pipeline::WORKSPACE,
            &session.collection_name,
            question,
            4,
        )
        .await
        .map_err(internal)?;
        let on_slide = req
            .slide_ordinal
            .and_then(|i| usize::try_from(i).ok())
            .and_then(|i| session.slides.get(i))
            .map(|s| {
                let said = s
                    .lines
                    .iter()
                    .map(|l| l.text.as_str())
                    .collect::<Vec<_>>()
                    .join(" ");
                format!(
                    "The listener is on the slide \"{}\", where the narration said: {said}\n\n",
                    s.title
                )
            })
            .unwrap_or_default();

        let provider = opennotebook_session::ai::provider()
            .await
            .map_err(internal)?;
        let model = opennotebook_session::settings::agent_model().await;
        let language = opennotebook_session::settings::language_rule(
            &opennotebook_session::settings::language().await,
        );
        let system = format!(
            "You answer a listener's question about a narrated learning session titled \"{}\". \
             Answer in two to four plain sentences, as you would say them aloud. Use only the \
             material given; when it does not cover the question, say so in one sentence. {language}",
            session.title
        );
        let user = format!(
            "{on_slide}Material from the session's sources:\n\n{}\nQuestion: {question}",
            grounding.as_context()
        );
        let resp = provider
            .completions()
            .model(&model)
            .message(Message::system(system))
            .user(user)
            .send()
            .await
            .map_err(|e| internal(format!("the model could not answer: {e}")))?;
        opennotebook_session::spend::record("ask", &model, resp.usage.as_ref());
        let answer = resp.text.trim().to_string();
        if answer.is_empty() {
            return Err(internal("the model returned no answer"));
        }
        Ok(SessionAskOutput { answer })
    }

    /// Remove a session: a deck or an audio overview.
    ///
    /// Its prep job is stopped first if it is still being made, then the row
    /// goes, and with it the output's place in its collection. What it left
    /// behind goes after the row, best effort: its deck, its audio and its
    /// ingest directory on disk, and its memory collection in the
    /// background. The row is the answer: a file that will not go is logged,
    /// never reported as a delete that failed, because the session the person
    /// asked to remove is gone from every list either way. See
    /// `collection::delete_output`.
    ///
    /// `false` means there was no such sid — not an error. Deleting something
    /// already gone is the state the caller wanted.
    async fn session_delete(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: SessionDeleteInput,
    ) -> Result<SessionDeleteOutput, opennotebook_api::RpcError> {
        let store = store().await?;
        let removed = crate::collection::delete_output(&store, &input.sid)
            .await
            .map_err(internal)?;
        Ok(SessionDeleteOutput { value: removed })
    }

    /// Rename a session.
    ///
    /// Read-modify-write on the whole row, because that is the only write the
    /// store has. Two people renaming the same session at the same moment is
    /// last-writer-wins; a title is not worth a lock.
    ///
    /// An empty title is refused rather than stored. A session with no title is
    /// unfindable in a gallery that sorts by title, and "" is not something a
    /// person means to set — it is what an untouched input box sends.
    async fn session_retitle(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: SessionRetitleInput,
    ) -> Result<SessionRetitleOutput, opennotebook_api::RpcError> {
        let req = input.req;
        let title = req.title.trim();
        if title.is_empty() {
            return Err(bad_request(
                "a session title cannot be empty: nothing in the gallery would name it".to_string(),
            ));
        }

        let store = store().await?;
        let Some(mut session) = store.get(&req.sid).await.map_err(internal)? else {
            return Ok(SessionRetitleOutput { value: false });
        };
        session.title = title.to_string();
        store.put(&session).await.map_err(internal)?;
        Ok(SessionRetitleOutput { value: true })
    }

    /// Pin or unpin a session.
    ///
    /// One method for both directions: the gallery's control is a toggle, and two
    /// methods would be two things to keep agreeing about what the absence of a
    /// call means.
    ///
    /// No limit is enforced here. The featured row shows four, but that is a
    /// presentation decision the page makes and can change; a server that
    /// refused a fifth pin would make the page's layout a storage rule.
    async fn session_pin(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: SessionPinInput,
    ) -> Result<SessionPinOutput, opennotebook_api::RpcError> {
        let req = input.req;
        let store = store().await?;
        let Some(mut session) = store.get(&req.sid).await.map_err(internal)? else {
            return Ok(SessionPinOutput { value: false });
        };
        session.pinned = req.pinned;
        store.put(&session).await.map_err(internal)?;
        Ok(SessionPinOutput { value: true })
    }

    async fn playback_get(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: crate::session::PlaybackGetInput,
    ) -> Result<crate::session::PlaybackGetOutput, opennotebook_api::RpcError> {
        Ok(head_out!(PlaybackGetOutput, playback::get(&input.sid)))
    }

    /// Start or resume AT a position.
    ///
    /// Resume takes the offset from the caller rather than from what pause
    /// stored, because the browser may have seeked in between and the element it
    /// is about to play from is the only thing that knows where it will start.
    async fn playback_play(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: crate::session::PlaybackPlayInput,
    ) -> Result<crate::session::PlaybackPlayOutput, opennotebook_api::RpcError> {
        Ok(head_out!(
            PlaybackPlayOutput,
            apply(input.req, PlayState::Playing)
        ))
    }

    /// Pause, recording the exact offset reported.
    ///
    /// §5: "Make pause work at an arbitrary offset and record the offset." The
    /// value is stored as given — never rounded to a line boundary — because an
    /// arbitrary millisecond is all phase 2 needs to resume.
    async fn playback_pause(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: crate::session::PlaybackPauseInput,
    ) -> Result<crate::session::PlaybackPauseOutput, opennotebook_api::RpcError> {
        Ok(head_out!(
            PlaybackPauseOutput,
            apply(input.req, PlayState::Paused)
        ))
    }

    async fn playback_progress(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: crate::session::PlaybackProgressInput,
    ) -> Result<crate::session::PlaybackProgressOutput, opennotebook_api::RpcError> {
        Ok(head_out!(
            PlaybackProgressOutput,
            apply(input.req, PlayState::Playing)
        ))
    }

    async fn playback_finish(
        &self,
        _ctx: &opennotebook_api::RequestContext,
        input: crate::session::PlaybackFinishInput,
    ) -> Result<crate::session::PlaybackFinishOutput, opennotebook_api::RpcError> {
        let mut h = playback::get(&input.sid);
        h.state = PlayState::Finished;
        Ok(head_out!(
            PlaybackFinishOutput,
            playback::put(&input.sid, h)
        ))
    }
}

/// Move the playhead and set its state, keeping the reported offset verbatim.
fn apply(req: PlaybackAtReq, state: PlayState) -> Head {
    let sid = req.sid.clone();
    playback::put(
        &sid,
        Head {
            slide_ordinal: req.slide_ordinal,
            line_id: req.line_id,
            offset_ms: req.offset_ms,
            state,
        },
    )
}

/// Every playback method returns the same four fields, but the macro generates a
/// distinct `<Method>Output` for each. One macro rather than five near-identical
/// constructors, so a field added to `Playhead` is one edit and cannot be
/// applied to four of the five.
macro_rules! head_out {
    ($t:ident, $h:expr) => {{
        let h: Head = $h;
        crate::session::$t {
            slide_ordinal: h.slide_ordinal,
            line_id: h.line_id,
            offset_ms: h.offset_ms,
            state: h.state.as_str().to_string(),
        }
    }};
}
use head_out;

/// Load one session for a non-RPC route.
///
/// The byte routes and the voice turn need the same document the RPC surface
/// reads, and they answer on HTTP rather than JSON-RPC, so they need it without
/// an `RpcError` wrapper. `Ok(None)` is a missing session, which is a different
/// thing from a failure to look — the discriminator the store already keeps
/// separate, carried through rather than flattened into an empty document.
/// A build's parameters once the studio's defaults are applied.
pub(crate) struct BuildPlan {
    pub staged: Vec<String>,
    pub speakers: Vec<Speaker>,
    pub slide_count: i64,
    pub style: String,
    /// Present when the request is for an audio overview.
    pub audio: Option<opennotebook_session::AudioSpec>,
}

/// What a prep builds once its defaults are applied: how many parts, how many
/// voices, and whether it is an audio overview.
///
/// The one rule for it. The pipeline builds this, and the spending limit and
/// the estimate price this, so the price cannot describe a different build.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Shape {
    /// Slides, or an audio overview's chapters.
    pub slides: u64,
    pub speakers: u64,
    pub audio: Option<opennotebook_session::AudioSpec>,
}

/// The shape of a build from its three deciding values. An audio overview's
/// format sets its chapters and caps its voices; a slide session takes the
/// count it was given, clamped into the settings' range, or the default.
pub(crate) fn shape(
    slide_count: Option<i64>,
    speakers: usize,
    audio: Option<opennotebook_session::AudioSpec>,
) -> Shape {
    let (slides, speakers) = match &audio {
        Some(a) => (a.chapters() as u64, speakers.min(a.format.speakers())),
        None => (
            opennotebook_session::settings::clamp_slides(
                slide_count.unwrap_or(opennotebook_session::settings::SLIDES_DEFAULT),
            ) as u64,
            speakers,
        ),
    };
    Shape {
        slides,
        speakers: speakers.max(1) as u64,
        audio,
    }
}

/// The shape of a prep request.
pub(crate) fn prep_shape(req: &crate::session::SessionPrepareReq) -> Shape {
    shape(
        req.slide_count,
        req.speakers.len(),
        audio_spec(
            req.audio_format.as_deref(),
            req.audio_length.as_deref(),
            req.focus.as_deref(),
        ),
    )
}

impl BuildPlan {
    pub fn shape(&self) -> Shape {
        shape(
            Some(self.slide_count),
            self.speakers.len(),
            self.audio.clone(),
        )
    }
}

/// An audio overview's spec from a request's three optional fields; None when
/// no format is named, which is a slide session.
pub(crate) fn audio_spec(
    format: Option<&str>,
    length: Option<&str>,
    focus: Option<&str>,
) -> Option<opennotebook_session::AudioSpec> {
    let format = format.map(str::trim).filter(|f| !f.is_empty())?;
    Some(opennotebook_session::AudioSpec {
        format: opennotebook_session::AudioFormat::parse(format),
        length: opennotebook_session::AudioLength::parse(length.unwrap_or_default()),
        focus: focus.unwrap_or_default().trim().to_string(),
    })
}

/// The studio settings a build plan reads, read once per plan.
#[derive(Debug, Clone)]
pub(crate) struct BuildDefaults {
    /// `auto`, `1` or `2`.
    pub speaker_count: String,
    pub host: Speaker,
    pub second: Speaker,
    /// Already in the slide range.
    pub slide_count: i64,
    pub style: String,
    pub audio_length: String,
}

impl BuildDefaults {
    pub(crate) async fn read() -> Self {
        use opennotebook_session::settings as set;
        Self {
            speaker_count: set::get(set::SPEAKER_COUNT_KEY).await,
            host: Speaker {
                speaker_id: "host".into(),
                voice_id: set::get(set::SPEAKER1_VOICE_KEY).await,
                display_name: set::get(set::SPEAKER1_NAME_KEY).await,
                role: set::get(set::SPEAKER1_ROLE_KEY).await,
            },
            second: Speaker {
                speaker_id: "expert".into(),
                voice_id: set::get(set::SPEAKER2_VOICE_KEY).await,
                display_name: set::get(set::SPEAKER2_NAME_KEY).await,
                role: set::get(set::SPEAKER2_ROLE_KEY).await,
            },
            slide_count: set::clamp_slides(
                set::get(set::SLIDE_COUNT_KEY)
                    .await
                    .parse()
                    .unwrap_or(set::SLIDES_DEFAULT),
            ),
            style: set::get(set::STYLE_KEY).await,
            audio_length: set::get(set::AUDIO_LENGTH_KEY).await,
        }
    }
}

/// What a build request asks for, before the defaults fill the gaps.
#[derive(Debug, Clone, Default)]
pub(crate) struct PlanAsk<'a> {
    pub speakers: Option<i64>,
    pub slide_count: Option<i64>,
    pub style: Option<&'a str>,
    pub audio_format: Option<&'a str>,
    pub audio_length: Option<&'a str>,
    pub focus: Option<&'a str>,
    /// How many sources it is built from, for the automatic speaker count.
    pub sources: usize,
}

/// A request with the defaults applied.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Planned {
    pub speakers: Vec<Speaker>,
    pub slide_count: i64,
    pub style: String,
    pub audio: Option<opennotebook_session::AudioSpec>,
}

/// The one rule for what a build makes: speakers, voices, slide count, style
/// and an audio overview's length, from the request where it says and the
/// settings where it does not.
///
/// A deck's slide count is clamped into the settings' range (3 to 12) whoever
/// asks, so an agent's request and the dialog's build the same thing.
pub(crate) fn plan(ask: &PlanAsk<'_>, d: &BuildDefaults) -> Result<Planned, String> {
    let length = ask
        .audio_length
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .unwrap_or(&d.audio_length);
    let audio = audio_spec(ask.audio_format, Some(length), ask.focus);
    let two = match ask.speakers {
        // An audio overview's format decides its voices: Brief is one host,
        // the rest are two.
        _ if audio.is_some() => audio.as_ref().is_some_and(|a| a.format.speakers() == 2),
        Some(1) => false,
        Some(2) => true,
        Some(n) => return Err(format!("speakers is 1 or 2, not {n}")),
        // A conversation needs something to converse about, and one source
        // rarely carries two voices.
        None => match d.speaker_count.as_str() {
            "1" => false,
            "2" => true,
            _ => ask.sources >= 2,
        },
    };
    let mut speakers = vec![d.host.clone()];
    if two {
        speakers.push(d.second.clone());
    }
    let slide_count = match (&audio, ask.slide_count) {
        (Some(a), _) => a.chapters() as i64,
        (None, Some(n)) => opennotebook_session::settings::clamp_slides(n),
        (None, None) => d.slide_count,
    };
    let style = match ask.style.map(str::trim).filter(|s| !s.is_empty()) {
        Some(s) if opennotebook_sdk::styles::style(s).is_none() => {
            return Err(format!("`{s}` is not a slide style; styles_list has them"));
        }
        Some(s) => s.to_string(),
        None => d.style.clone(),
    };
    Ok(Planned {
        speakers,
        slide_count,
        style,
        audio,
    })
}

/// The defaults a build takes for what its request leaves out, by [`plan`].
/// One function, so `session_build` and `session_estimate` cannot disagree
/// about what they are building.
pub(crate) async fn build_plan(
    req: &crate::session::SessionBuildReq,
) -> Result<BuildPlan, opennotebook_api::RpcError> {
    let from = source_sid(req);
    crate::create::safe_sid(from).map_err(bad_request)?;
    let staged = crate::create::staged_names(from);
    if staged.is_empty() {
        return Err(bad_request(format!(
            "collection `{from}` has no sources; add some with source_add_urls, source_add_text or deep_research first"
        )));
    }
    let planned = plan(
        &PlanAsk {
            speakers: req.speakers,
            slide_count: req.slide_count,
            style: req.style.as_deref(),
            audio_format: req.audio_format.as_deref(),
            audio_length: req.audio_length.as_deref(),
            focus: req.focus.as_deref(),
            sources: staged.len(),
        },
        &BuildDefaults::read().await,
    )
    .map_err(bad_request)?;
    Ok(BuildPlan {
        staged,
        speakers: planned.speakers,
        slide_count: planned.slide_count,
        style: planned.style,
        audio: planned.audio,
    })
}

/// A prep request with the studio's defaults applied, by the same [`plan`] as
/// `session_build`: an empty speaker list, an absent slide count, style or
/// audio length take the settings, and a slide count is clamped into range.
/// Speakers it was given are kept as they are, so a retry sounds like the
/// output it retries. A style this build does not know takes the setting
/// rather than refusing, so a retry of an output whose style was retired
/// still builds.
async fn prep_defaults(
    req: &mut crate::session::SessionPrepareReq,
    sources: usize,
) -> Result<(), String> {
    let known_style = req
        .style
        .as_deref()
        .filter(|s| opennotebook_sdk::styles::style(s.trim()).is_some());
    let planned = plan(
        &PlanAsk {
            speakers: None,
            slide_count: req.slide_count,
            style: known_style,
            audio_format: req.audio_format.as_deref(),
            audio_length: req.audio_length.as_deref(),
            focus: req.focus.as_deref(),
            sources,
        },
        &BuildDefaults::read().await,
    )?;
    if req.speakers.is_empty() {
        req.speakers = planned.speakers;
    }
    req.slide_count = Some(planned.slide_count);
    req.style = Some(planned.style);
    if let Some(a) = &planned.audio {
        req.audio_length = Some(a.length().id().to_string());
    }
    Ok(())
}

/// The style a prep's row keeps: a deck's, and none for an audio overview,
/// which has no slides to style.
pub(crate) fn deck_style(req: &crate::session::SessionPrepareReq) -> Option<String> {
    if req
        .audio_format
        .as_deref()
        .is_some_and(|f| !f.trim().is_empty())
    {
        return None;
    }
    req.style.clone().filter(|s| !s.trim().is_empty())
}

/// Whose staged sources a build reads: its collection's when it names one,
/// else its own sid's, which is the collection of a build from before
/// collections.
pub(crate) fn source_sid(req: &crate::session::SessionBuildReq) -> &str {
    req.collection
        .as_deref()
        .map(str::trim)
        .filter(|c| !c.is_empty())
        .unwrap_or(&req.sid)
}

/// In a collection each output is its own session, so an empty sid is the
/// caller asking for a fresh one. Without a collection the sid IS the
/// collection, as before collections, and stays required.
fn wants_fresh_sid(req: &crate::session::SessionBuildReq) -> bool {
    req.collection.is_some() && req.sid.trim().is_empty()
}

/// An output's title: the one given, else one that tells it from the others
/// in its collection — "Editorial slides", "Brief audio overview · <focus>".
/// Never empty: an untitled card cannot be found in a list sorted by title.
fn output_title(
    given: &str,
    style: Option<&str>,
    audio: Option<&opennotebook_session::AudioSpec>,
) -> String {
    let given = crate::create::one_line(given);
    if !given.is_empty() {
        return given;
    }
    let named = match audio {
        Some(a) if a.focus.trim().is_empty() => format!("{} audio overview", a.format.label()),
        Some(a) => format!(
            "{} audio overview · {}",
            a.format.label(),
            crate::create::one_line(&a.focus)
        ),
        None => format!(
            "{} slides",
            style
                .and_then(opennotebook_sdk::styles::style)
                .unwrap_or(opennotebook_sdk::styles::DEFAULT_STYLE)
                .label
        ),
    };
    crate::create::clip_title(&named)
}

/// A session as a list shows it.
pub(crate) fn summary(s: opennotebook_session::Session) -> SessionSummary {
    SessionSummary {
        created_ms: created_ms(&s.sid) as i64,
        description: first_line(&s),
        collection: s.collection_id().to_string(),
        state: state_str(s.state).to_string(),
        slide_count: s.slides.len() as i64,
        speakers: s.speakers.len() as i64,
        kind: if s.audio.is_some() {
            "audio"
        } else {
            "session"
        }
        .to_string(),
        audio_format: s
            .audio
            .as_ref()
            .map(|a| a.format.id().to_string())
            .unwrap_or_default(),
        duration_ms: s
            .slides
            .iter()
            .flat_map(|sl| &sl.lines)
            .filter_map(|l| l.duration_ms.map(|d| d.millis()))
            .sum::<u64>() as i64,
        pinned: s.pinned,
        spent_usd: s.spent_usd.unwrap_or(0.0),
        spent_known: s.spent_usd.is_some(),
        sid: s.sid,
        title: s.title,
    }
}

pub async fn load_session(sid: &str) -> Result<Option<opennotebook_session::Session>, String> {
    crate::collection::open_store()
        .await?
        .get(sid)
        .await
        .map_err(|e| format!("session read failed: {e}"))
}

/// The first spoken line, as a preview of the session.
fn first_line(s: &opennotebook_session::Session) -> String {
    s.slides_in_order()
        .first()
        .and_then(|sl| sl.lines_in_order().first().map(|l| l.text.clone()))
        .unwrap_or_default()
}

/// Creation time out of the sid, where there is one.
///
/// Every sid the studio mints ends in a millisecond timestamp — `s1789410658063`
/// from the create flow, `tbld1789037336735` and `two1789048731` from the test
/// fixtures. A trailing run of 13 digits in the plausible range is a timestamp;
/// anything else is a name somebody chose and carries no date at all.
pub(crate) fn created_ms(sid: &str) -> u64 {
    let digits: String = sid
        .chars()
        .rev()
        .take_while(|c| c.is_ascii_digit())
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    // Both widths are real in this store: the create flow mints milliseconds
    // (`s1789410658063`) and the earlier fixtures minted seconds
    // (`two1789048731`). Reading only 13 digits left most sessions dateless.
    let Ok(n) = digits.parse::<u64>() else {
        return 0;
    };
    let ms = match digits.len() {
        13 => n,
        10 => n * 1000,
        _ => return 0,
    };
    // Sanity, not superstition: reject anything before 2001 or absurdly far
    // ahead, so a run of digits that is not a time does not become a 1970 date.
    if (1_000_000_000_000..=4_000_000_000_000).contains(&ms) {
        ms
    } else {
        0
    }
}

#[cfg(test)]
mod created_tests {
    #[test]
    fn a_sid_yields_its_creation_time_or_nothing() {
        // Milliseconds, as the create flow mints them.
        assert_eq!(super::created_ms("s1789410658063"), 1_789_410_658_063);
        assert_eq!(super::created_ms("tbld1789037336735"), 1_789_037_336_735);
        // Seconds, as the earlier fixtures did. Reading only 13 digits left
        // most of the store dateless.
        assert_eq!(super::created_ms("two1789048731"), 1_789_048_731_000);
        assert_eq!(super::created_ms("png1789052449"), 1_789_052_449_000);
        // No timestamp at all is 0, never a guess and never 1970.
        assert_eq!(super::created_ms("e2e"), 0);
        assert_eq!(super::created_ms("my-notes"), 0);
        assert_eq!(super::created_ms("v2"), 0);
    }
}

/// A `Preparing` row whose prep job is no longer alive, marked `Failed`.
///
/// A prep that errors writes its own failure. One that is killed — the server
/// restarted, the job cancelled, the box rebooted — writes nothing, and its row
/// would say `preparing` forever: a card spinning on the gallery and a progress
/// screen that never moves. The job row is the authority on whether the work is
/// still happening, so it is asked. Anything short of a clear "it stopped"
/// (the database unreadable, the job still queued) leaves the row alone.
pub async fn reconcile(
    store: &SessionStore,
    s: opennotebook_session::Session,
) -> opennotebook_session::Session {
    if s.state != SessionState::Preparing {
        return s;
    }
    let Some(job_sid) = s.prep_job_sid.clone() else {
        return s;
    };
    let Some(status) = job_status(&job_sid).await else {
        return s;
    };
    if !matches!(status.as_str(), "failed" | "cancelled" | "inactive") {
        return s;
    }
    // Re-read before writing: the prep may have recorded its own, better
    // reason between the two calls, or the row may have been deleted.
    let now = store.get(&s.sid).await.map_err(|_| ());
    let wanted = crate::collection::still_wanted(store, s.collection.as_deref()).await;
    let (s, write) = settle(s, &status, &job_sid, now, wanted);
    if write {
        let _ = store.put(&s).await;
    }
    s
}

/// What reconciling a `Preparing` row whose job stopped comes to, and whether
/// it is written: `Failed`, written, only onto a row that is still there,
/// still `Preparing`, and in a collection that is still there. A deleted row
/// written back would undelete the output; one the store could not read is
/// left for the next look.
fn settle(
    mut s: opennotebook_session::Session,
    status: &str,
    job_sid: &str,
    now: Result<Option<opennotebook_session::Session>, ()>,
    collection_wanted: bool,
) -> (opennotebook_session::Session, bool) {
    match now {
        Ok(Some(now)) if now.state != SessionState::Preparing => (now, false),
        Ok(Some(_)) if collection_wanted => {
            s.state = SessionState::Failed;
            s.failure = Some(format!(
                "the prep job stopped ({status}) before it finished (prep job {job_sid})"
            ));
            (s, true)
        }
        Ok(_) | Err(()) => (s, false),
    }
}

/// A job's status. A job with no row at all is `inactive`: nothing is doing
/// the work, and nothing ever will.
async fn job_status(job_sid: &str) -> Option<String> {
    match opennotebook_build::job::get(job_sid).await {
        Ok(Some(j)) => Some(j.status),
        Ok(None) => Some("inactive".into()),
        Err(_) => None,
    }
}

async fn store() -> Result<SessionStore, opennotebook_api::RpcError> {
    crate::collection::open_store().await.map_err(internal)
}

/// An upstream or storage failure. Distinct from [`bad_request`] so a caller can
/// tell "you asked for something impossible" from "this service could not do it".
fn internal(e: impl std::fmt::Display) -> opennotebook_api::RpcError {
    opennotebook_api::RpcError::internal(e.to_string())
}

fn bad_request(msg: String) -> opennotebook_api::RpcError {
    opennotebook_api::RpcError::invalid_params(msg)
}

/// One slide's rendered bytes, and what they are.
///
/// A type rather than a `String`, because the previous shape returned only HTML
/// and the caller had no way to notice that a png deck had given it nothing.
pub enum Render {
    Html(String),
    Png(Vec<u8>),
}

/// Read a slide's render off disk.
///
/// Studio writes its own slides to `decks/<sid>/studio/<slide>.html`. Decks
/// built before the studio wrote its own slides, by an earlier slide service,
/// keep that service's layout under the same deck root —
/// `decks/<sid>/<presentation>/<slide>/output/slide.html`, or `slide.png` for a
/// png deck — and are read from there. Nothing here talks to another service.
pub async fn slide_render(r: &SlideRef) -> anyhow::Result<Render> {
    for part in [&r.collection, &r.presentation, &r.slide] {
        anyhow::ensure!(
            !part.is_empty() && !part.contains("..") && !part.contains('/') && !part.contains('\\'),
            "slide ref `{}/{}/{}` is not a plain name",
            r.collection,
            r.presentation,
            r.slide
        );
    }
    let deck = crate::pipeline::Layout::in_data_dir()
        .decks_root
        .join(&r.collection);
    let own = deck.join(&r.presentation).join(format!("{}.html", r.slide));
    let old = deck.join(&r.presentation).join(&r.slide).join("output");
    if let Ok(html) = tokio::fs::read_to_string(&own).await {
        return Ok(Render::Html(html));
    }
    if let Ok(html) = tokio::fs::read_to_string(old.join("slide.html")).await {
        return Ok(Render::Html(html));
    }
    if let Ok(png) = tokio::fs::read(old.join("slide.png")).await {
        return Ok(Render::Png(png));
    }
    anyhow::bail!(
        "slide `{}` has no render on disk under {}",
        r.slide,
        deck.display()
    )
}

fn state_str(s: SessionState) -> &'static str {
    match s {
        SessionState::Preparing => "preparing",
        SessionState::Ready => "ready",
        SessionState::Failed => "failed",
    }
}

/// A legal single path segment and a legal collection name at once — the
/// session directory's basename becomes the collection name, so both
/// constraints apply to one value.
fn legal_sid(sid: &str) -> bool {
    !sid.is_empty()
        && !sid.starts_with('.')
        && sid
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

/// Map the stored session onto the wire type, adding the two url fields.
///
/// The urls are the whole point of this service: `audio_path` is an absolute
/// path on this box and unreachable from a browser, so a player reads
/// `audio_url` instead. Both are carried — the path is what the prep job wrote,
/// the url is what the surface exposes.
fn wire(s: opennotebook_session::Session) -> Session {
    Session {
        collection: s.collection.clone(),
        spent_usd: s.spent_usd,
        style: s.style.clone(),
        sid: s.sid.clone(),
        title: s.title,
        collection_name: s.collection_name,
        collection_sid: s.collection_sid,
        deck_ref: s.deck_ref.map(wire_deck),
        speakers: s
            .speakers
            .into_iter()
            .map(|p| Speaker {
                speaker_id: p.speaker_id.0,
                voice_id: p.voice_id,
                display_name: p.display_name,
                role: p.role,
            })
            .collect(),
        slides: s
            .slides
            .into_iter()
            .map(|sl| SessionSlide {
                slide_url: format!("{SLIDE_ROUTE}?session={}&ordinal={}", s.sid, sl.ordinal),
                title: sl.title,
                slide_ref: wire_ref(sl.slide_ref),
                ordinal: sl.ordinal as i64,
                aspect: Aspect {
                    width: sl.aspect.width as i64,
                    height: sl.aspect.height as i64,
                },
                lines: sl
                    .lines
                    .into_iter()
                    .map(|l| NarrationLine {
                        // Present only when the line has been synthesised. A url
                        // for a line with no audio would 404, and an absent
                        // field says "not yet" where a dead link says nothing.
                        audio_url: l.audio_path.as_ref().map(|_| {
                            format!("{AUDIO_ROUTE}?session={}&line={}", s.sid, l.line_id.0)
                        }),
                        line_id: l.line_id.0,
                        speaker_id: l.speaker_id.0,
                        ordinal: l.ordinal as i64,
                        text: l.text,
                        audio_path: l.audio_path,
                        duration_ms: l.duration_ms.map(|d| d.millis() as i64),
                        cues: l
                            .cues
                            .into_iter()
                            .map(|c| Cue {
                                at_secs: c.at_secs,
                                end_secs: c.end_secs,
                                text: c.text,
                            })
                            .collect(),
                    })
                    .collect(),
            })
            .collect(),
        state: state_str(s.state).to_string(),
        prep_job_sid: s.prep_job_sid,
        failure: s.failure,
        audio: s.audio.map(|a| crate::session::AudioOverview {
            format: a.format.id().to_string(),
            length: a.length().id().to_string(),
            minutes: a.minutes() as i64,
            focus: a.focus,
        }),
    }
}

fn wire_deck(r: StoreDeckRef) -> DeckRef {
    DeckRef {
        collection: r.collection,
        presentation: r.presentation,
    }
}

fn wire_ref(r: SlideRef) -> crate::session::SlideRef {
    crate::session::SlideRef {
        collection: r.collection,
        presentation: r.presentation,
        slide: r.slide,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec(format: &str, length: &str) -> Option<opennotebook_session::AudioSpec> {
        audio_spec(Some(format), Some(length), None)
    }

    /// The spending limit priced an audio overview on the slide count it was
    /// sent, while the pipeline built its format's chapters. One rule now.
    #[test]
    fn an_audio_overview_is_shaped_by_its_format_not_the_slide_count() {
        let deep = shape(Some(12), 2, spec("deep_dive", "longer"));
        assert_eq!(deep.slides, 6);
        assert_eq!(deep.speakers, 2);
        let brief = shape(Some(12), 2, spec("brief", ""));
        assert_eq!((brief.slides, brief.speakers), (2, 1), "Brief is one host");
    }

    #[test]
    fn a_slide_session_takes_its_count_in_range_or_the_default() {
        assert_eq!(shape(Some(9), 2, None).slides, 9);
        assert_eq!(shape(Some(0), 2, None).slides, 3);
        assert_eq!(shape(Some(30), 2, None).slides, 12);
        assert_eq!(
            shape(None, 0, None),
            Shape {
                slides: 5,
                speakers: 1,
                audio: None,
            }
        );
    }

    fn defaults() -> BuildDefaults {
        BuildDefaults {
            speaker_count: "auto".into(),
            host: Speaker {
                speaker_id: "host".into(),
                ..Default::default()
            },
            second: Speaker {
                speaker_id: "expert".into(),
                ..Default::default()
            },
            slide_count: 6,
            style: "editorial".into(),
            audio_length: "longer".into(),
        }
    }

    /// One range, whoever asks: the dialog's 3 to 12, clamped rather than
    /// refused, and the setting when the request says nothing.
    #[test]
    fn a_plan_clamps_the_slide_count_and_fills_the_gaps() {
        let d = defaults();
        let n = |slide_count| {
            plan(
                &PlanAsk {
                    slide_count,
                    sources: 1,
                    ..Default::default()
                },
                &d,
            )
            .unwrap()
            .slide_count
        };
        assert_eq!(n(Some(1)), 3);
        assert_eq!(n(Some(30)), 12);
        assert_eq!(n(Some(8)), 8);
        assert_eq!(n(None), 6);

        let p = plan(&PlanAsk::default(), &d).unwrap();
        assert_eq!(p.style, "editorial");
        assert_eq!(p.speakers.len(), 1, "auto with one source is one voice");
        let p = plan(
            &PlanAsk {
                sources: 2,
                ..Default::default()
            },
            &d,
        )
        .unwrap();
        assert_eq!(p.speakers.len(), 2);
        assert!(
            plan(
                &PlanAsk {
                    style: Some("nope"),
                    ..Default::default()
                },
                &d
            )
            .is_err()
        );
        assert!(
            plan(
                &PlanAsk {
                    speakers: Some(3),
                    ..Default::default()
                },
                &d
            )
            .is_err()
        );
    }

    /// An audio overview takes the length setting when it names none, and
    /// its format's voices whatever the speaker setting says.
    #[test]
    fn a_plan_gives_an_audio_overview_the_length_setting() {
        let d = defaults();
        let p = plan(
            &PlanAsk {
                audio_format: Some("deep_dive"),
                speakers: Some(1),
                ..Default::default()
            },
            &d,
        )
        .unwrap();
        let a = p.audio.unwrap();
        assert_eq!(a.length().id(), "longer");
        assert_eq!(p.slide_count, a.chapters() as i64);
        assert_eq!(p.speakers.len(), 2);
        let p = plan(
            &PlanAsk {
                audio_format: Some("deep_dive"),
                audio_length: Some("shorter"),
                ..Default::default()
            },
            &d,
        )
        .unwrap();
        assert_eq!(p.audio.unwrap().length().id(), "shorter");
    }

    /// `session_build` hands its plan to `session_prepare` as a slide count and
    /// speakers; the estimate prices the plan. Both must come out the same.
    #[test]
    fn the_estimate_and_the_limit_price_the_same_build() {
        let audio = spec("debate", "default");
        let plan = BuildPlan {
            staged: vec!["a.md".into()],
            speakers: vec![Speaker::default(), Speaker::default()],
            slide_count: audio.as_ref().unwrap().chapters() as i64,
            style: String::new(),
            audio: audio.clone(),
        };
        assert_eq!(
            plan.shape(),
            shape(Some(plan.slide_count), plan.speakers.len(), audio)
        );
        assert_eq!(plan.shape().slides, 4);
    }

    #[test]
    fn a_sid_that_would_collide_with_a_path_is_refused() {
        assert!(legal_sid("s-123"));
        assert!(legal_sid("session_1"));
        // These are the ones that matter: each would name a different directory
        // than the caller meant, and the collection follows the directory.
        assert!(!legal_sid(""));
        assert!(!legal_sid(".hidden"));
        assert!(!legal_sid("a/b"));
        assert!(!legal_sid(".."));
        assert!(!legal_sid("a b"));
    }

    #[test]
    fn a_line_with_no_audio_gets_no_url() {
        // A url that 404s says nothing; an absent field says "not synthesised
        // yet", which is a real state during prep.
        use opennotebook_session::{LineId, SpeakerId};
        let line = opennotebook_session::NarrationLine {
            line_id: LineId("l1".into()),
            speaker_id: SpeakerId("s1".into()),
            ordinal: 0,
            text: "hello".into(),
            audio_path: None,
            duration_ms: None,
            cues: vec![],
        };
        let s = opennotebook_session::Session {
            sid: "sess".into(),
            title: "t".into(),
            collection_name: "sess".into(),
            collection_sid: None,
            deck_ref: None,
            speakers: vec![],
            slides: vec![opennotebook_session::SessionSlide {
                slide_ref: SlideRef {
                    collection: "c".into(),
                    presentation: "p".into(),
                    slide: "1".into(),
                },
                title: String::new(),
                on_slide: Vec::new(),
                ordinal: 0,
                aspect: opennotebook_session::Aspect {
                    width: 1920,
                    height: 1080,
                },
                lines: vec![line],
            }],
            state: SessionState::Preparing,
            prep_job_sid: None,
            failure: None,
            pinned: false,
            audio: None,
            collection: None,
            spent_usd: None,
            style: Some("vector".into()),
        };
        let w = wire(s);
        assert_eq!(w.style.as_deref(), Some("vector"));
        assert!(w.slides[0].lines[0].audio_url.is_none());
        // The slide url is always present: the slide is written as soon as
        // the deck is, independently of synthesis.
        assert_eq!(
            w.slides[0].slide_url,
            "/api/session/slide?session=sess&ordinal=0"
        );
    }
}

#[cfg(test)]
mod lifecycle_tests {
    use super::*;

    fn row(state: &str) -> opennotebook_session::Session {
        serde_json::from_value(serde_json::json!({
            "sid": "s1", "title": "t", "collection_name": "s1", "collection_sid": null,
            "deck_ref": null, "speakers": [], "slides": [], "state": state,
            "prep_job_sid": "j1", "collection": "c1"
        }))
        .unwrap()
    }

    #[test]
    fn a_dead_job_fails_its_row_only_while_the_row_is_there() {
        let (s, write) = settle(
            row("preparing"),
            "failed",
            "j1",
            Ok(Some(row("preparing"))),
            true,
        );
        assert!(write);
        assert_eq!(s.state, SessionState::Failed);
        assert!(s.failure.unwrap().contains("prep job j1"));

        // Deleted while its job died: writing it back would undelete it.
        let (s, write) = settle(row("preparing"), "cancelled", "j1", Ok(None), true);
        assert!(!write);
        assert_eq!(s.state, SessionState::Preparing);

        // Its collection was deleted: the same.
        let (_, write) = settle(
            row("preparing"),
            "failed",
            "j1",
            Ok(Some(row("preparing"))),
            false,
        );
        assert!(!write);

        // The prep wrote its own outcome in between: that stands.
        let (s, write) = settle(
            row("preparing"),
            "failed",
            "j1",
            Ok(Some(row("ready"))),
            true,
        );
        assert!(!write);
        assert_eq!(s.state, SessionState::Ready);

        // Unreadable: left for the next look.
        let (_, write) = settle(row("preparing"), "failed", "j1", Err(()), true);
        assert!(!write);
    }

    #[test]
    fn a_build_with_no_title_is_named_by_what_tells_it_apart() {
        let audio = |format: &str, focus: &str| audio_spec(Some(format), None, Some(focus));
        assert_eq!(output_title(" Mine ", Some("editorial"), None), "Mine");
        assert_eq!(output_title("Two\nlines", None, None), "Twolines");
        let editorial = opennotebook_sdk::styles::style("editorial").unwrap().label;
        assert_eq!(
            output_title("  ", Some("editorial"), None),
            format!("{editorial} slides")
        );
        assert_eq!(
            output_title("", None, None),
            format!("{} slides", opennotebook_sdk::styles::DEFAULT_STYLE.label)
        );
        assert_eq!(
            output_title("", None, audio("brief", "").as_ref()),
            "Brief audio overview"
        );
        assert_eq!(
            output_title("", None, audio("debate", " the latency claims ").as_ref()),
            "Debate audio overview · the latency claims"
        );
        let long = output_title("", None, audio("deep_dive", &"word ".repeat(40)).as_ref());
        assert!(
            long.starts_with("Deep Dive audio overview · word"),
            "{long}"
        );
        assert!(long.chars().count() <= 72, "{long}");
    }

    #[test]
    fn an_empty_sid_mints_one_only_in_a_collection() {
        let req = |sid: &str, collection: Option<&str>| crate::session::SessionBuildReq {
            sid: sid.into(),
            collection: collection.map(str::to_string),
            ..serde_json::from_value(serde_json::json!({ "sid": "" })).unwrap()
        };
        assert!(wants_fresh_sid(&req("", Some("c1"))));
        assert!(wants_fresh_sid(&req("  ", Some("c1"))));
        assert!(
            !wants_fresh_sid(&req("s9", Some("c1"))),
            "a sid given is kept"
        );
        assert!(
            !wants_fresh_sid(&req("", None)),
            "without a collection the sid is the collection, and required"
        );
    }
}
