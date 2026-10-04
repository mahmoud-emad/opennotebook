use serde_json::{Value, json};

use crate::notes::*;
use crate::{RequestContext, RpcError};

/// A notes service that answers from fixed data, to drive `dispatch`.
struct Stub;

#[async_trait::async_trait]
impl NotesServiceApi for Stub {
    async fn notes_create(
        &self,
        _: &RequestContext,
        i: NotesCreateInput,
    ) -> Result<NotesCreateOutput, RpcError> {
        Ok(NotesCreateOutput {
            sid: i.req.sid,
            focus: i.req.focus.unwrap_or_default(),
            ..Default::default()
        })
    }
    async fn notes_estimate(
        &self,
        _: &RequestContext,
        _: NotesEstimateInput,
    ) -> Result<NotesEstimateOutput, RpcError> {
        Err(RpcError::internal(
            "the AI account is out of credit: HTTP 402",
        ))
    }
    async fn notes_list(
        &self,
        _: &RequestContext,
        _: NotesListInput,
    ) -> Result<NotesListOutput, RpcError> {
        Ok(NotesListOutput::default())
    }
    async fn notes_list_all(
        &self,
        _: &RequestContext,
        _: NotesListAllInput,
    ) -> Result<NotesListAllOutput, RpcError> {
        Ok(NotesListAllOutput::default())
    }
    async fn notes_get(
        &self,
        _: &RequestContext,
        _: NotesGetInput,
    ) -> Result<NotesGetOutput, RpcError> {
        Ok(NotesGetOutput::default())
    }
    async fn notes_delete(
        &self,
        _: &RequestContext,
        i: NotesDeleteInput,
    ) -> Result<NotesDeleteOutput, RpcError> {
        Ok(NotesDeleteOutput {
            value: i.req.id == "there",
        })
    }
    async fn notes_retitle(
        &self,
        _: &RequestContext,
        _: NotesRetitleInput,
    ) -> Result<NotesRetitleOutput, RpcError> {
        Ok(NotesRetitleOutput { value: true })
    }
}

async fn rpc(body: Value) -> Option<Value> {
    crate::handle(
        body.to_string().as_bytes(),
        SERVICE,
        VERSION,
        OPENRPC_JSON,
        |m, p| async move { dispatch(&Stub, &RequestContext::default(), &m, p).await },
    )
    .await
}

#[test]
fn optional_fields_are_omitted_and_may_be_absent() {
    let r: NotesCreateReq = serde_json::from_value(json!({ "sid": "s1" })).unwrap();
    assert_eq!((r.focus, r.sources), (None, None));
    assert_eq!(
        serde_json::to_value(NotesCreateReq {
            sid: "s1".into(),
            ..Default::default()
        })
        .unwrap(),
        json!({ "sid": "s1" })
    );
    // A required field is required.
    assert!(serde_json::from_value::<NotesCreateReq>(json!({})).is_err());
    // Integers are i64, whatever width the schema names.
    let c = NoteCitation {
        n: -1,
        ..Default::default()
    };
    assert_eq!(c.n, -1i64);
}

#[test]
fn a_scalar_result_is_bare_and_a_type_result_is_its_fields() {
    assert_eq!(
        serde_json::to_value(NotesDeleteOutput { value: true }).unwrap(),
        json!(true)
    );
    let out: NotesListOutput = serde_json::from_value(json!({})).unwrap();
    assert!(out.notes.is_empty(), "outputs default missing fields");
}

#[tokio::test]
async fn a_call_is_dispatched_by_name_with_named_params() {
    let got = rpc(json!({ "jsonrpc": "2.0", "id": 7, "method": "notes_delete",
                          "params": { "req": { "sid": "s", "id": "there" } } }))
    .await
    .unwrap();
    assert_eq!(got, json!({ "jsonrpc": "2.0", "id": 7, "result": true }));

    let got = rpc(
        json!({ "jsonrpc": "2.0", "id": "a", "method": "notes_create",
                          "params": { "req": { "sid": "s9", "focus": "kernels" } } }),
    )
    .await
    .unwrap();
    assert_eq!(got["result"]["sid"], "s9");
    assert_eq!(got["result"]["focus"], "kernels");
}

#[tokio::test]
async fn a_method_with_no_params_takes_none() {
    for params in [None, Some(Value::Null), Some(json!({}))] {
        let mut req = json!({ "jsonrpc": "2.0", "id": 1, "method": "notes_list_all" });
        if let Some(p) = params {
            req["params"] = p;
        }
        assert_eq!(rpc(req).await.unwrap()["result"], json!({ "notes": [] }));
    }
}

#[tokio::test]
async fn errors_carry_the_methods_own_words() {
    let got = rpc(
        json!({ "jsonrpc": "2.0", "id": 1, "method": "notes_estimate", "params": { "sid": "s" } }),
    )
    .await
    .unwrap();
    assert_eq!(
        got["error"],
        json!({ "code": -32603, "message": "the AI account is out of credit: HTTP 402" })
    );

    let got = rpc(json!({ "jsonrpc": "2.0", "id": 1, "method": "nope" }))
        .await
        .unwrap();
    assert_eq!(got["error"]["code"], -32601);
    assert_eq!(got["error"]["message"], "method not found: nope");

    let got =
        rpc(json!({ "jsonrpc": "2.0", "id": 1, "method": "notes_get", "params": { "req": 5 } }))
            .await
            .unwrap();
    assert_eq!(got["error"]["code"], -32602);
    assert!(
        got["error"]["message"]
            .as_str()
            .unwrap()
            .starts_with("params: ")
    );

    let got = rpc(json!({ "jsonrpc": "1.0", "id": 1, "method": "notes_list_all" }))
        .await
        .unwrap();
    assert_eq!(got["error"]["code"], -32600);

    let got = crate::handle(b"{not json", SERVICE, VERSION, OPENRPC_JSON, |_, _| async {
        Ok(Value::Null)
    })
    .await
    .unwrap();
    assert_eq!(got["error"]["code"], -32700);
}

#[tokio::test]
async fn notifications_get_nothing_and_batches_get_each_answer() {
    assert_eq!(
        rpc(json!({ "jsonrpc": "2.0", "method": "notes_list_all" })).await,
        None
    );
    let got = rpc(json!([
        { "jsonrpc": "2.0", "id": 1, "method": "notes_list_all" },
        { "jsonrpc": "2.0", "method": "notes_list_all" },
        { "jsonrpc": "2.0", "id": 2, "method": "rpc.health" }
    ]))
    .await
    .unwrap();
    assert_eq!(got.as_array().unwrap().len(), 2);
    assert_eq!(got[1]["result"]["service"], "NotesService");
}

#[tokio::test]
async fn discover_returns_the_openrpc_document_of_every_method() {
    let got = rpc(json!({ "jsonrpc": "2.0", "id": 1, "method": "rpc.discover" }))
        .await
        .unwrap();
    let names: Vec<&str> = got["result"]["methods"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, METHODS.iter().map(|m| m.0).collect::<Vec<_>>());
    assert!(
        got["result"]["components"]["schemas"]["StudyNotes"]["required"]
            .as_array()
            .unwrap()
            .contains(&json!("citations"))
    );
    let create = &got["result"]["methods"][0];
    assert_eq!(
        create["params"][0]["schema"]["$ref"],
        "#/components/schemas/NotesCreateReq"
    );
}

#[test]
fn every_domain_is_generated() {
    for (svc, n) in [
        (crate::mindmap::SERVICE, crate::mindmap::METHODS.len()),
        (crate::notes::SERVICE, crate::notes::METHODS.len()),
        (crate::session::SERVICE, crate::session::METHODS.len()),
        (crate::settings::SERVICE, crate::settings::METHODS.len()),
        (crate::sources::SERVICE, crate::sources::METHODS.len()),
    ] {
        assert!(n > 0, "{svc}");
    }
    let total = crate::mindmap::METHODS.len()
        + crate::notes::METHODS.len()
        + crate::session::METHODS.len()
        + crate::settings::METHODS.len()
        + crate::sources::METHODS.len();
    assert_eq!(total, 47, "the schema's 47 methods");
}
