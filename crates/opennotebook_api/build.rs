//! Generates the API from `oschema/<domain>/<domain>.oschema`.
//!
//! The schema is the contract: what an agent reads as a tool list, what the
//! server implements and what the browser calls. For each domain this emits,
//! into `$OUT_DIR/api.rs`, a module named after the domain's directory with:
//!
//! * one struct per schema type, fields as written; `x?:` fields are
//!   `Option<T>` that are omitted when `None`, and every integer is `i64`;
//! * per method, `<Method>Input` (one field per parameter) and `<Method>Output`
//!   (the result type's fields, or `{ value }` for a scalar, transparent on the
//!   wire);
//! * the `<Service>Api` trait the server implements, and `dispatch`, which
//!   routes a JSON-RPC method name and params to it;
//! * `<Service>Client`, the typed client;
//! * `OPENRPC_JSON`, the domain's OpenRPC document.
//!
//! The format is a small subset: types of `name: type  # doc` fields, and one
//! `service Name { version, description, methods }` block per file. Types are
//! `str`, `bool`, `f64`, the integer types, `[T]` and other type names.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use serde_json::{Value, json};

#[derive(Debug, Clone)]
struct Field {
    name: String,
    ty: String,
    optional: bool,
    doc: String,
}

#[derive(Debug, Clone)]
struct Type {
    name: String,
    doc: String,
    fields: Vec<Field>,
}

#[derive(Debug, Clone)]
struct Method {
    name: String,
    params: Vec<(String, String)>,
    result: String,
    doc: String,
}

#[derive(Debug, Default)]
struct Service {
    name: String,
    version: String,
    description: String,
    methods: Vec<Method>,
}

struct Domain {
    module: String,
    types: Vec<Type>,
    service: Service,
}

/// `code  # comment` split at the first `#` that is not inside a string.
fn split_comment(line: &str) -> (&str, &str) {
    let mut quoted = false;
    for (i, c) in line.char_indices() {
        match c {
            '"' => quoted = !quoted,
            '#' if !quoted => return (line[..i].trim(), line[i + 1..].trim()),
            _ => {}
        }
    }
    (line.trim(), "")
}

fn parse(path: &Path) -> (Vec<Type>, Service) {
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    let mut types = Vec::new();
    let mut service = Service::default();
    let mut pending_doc: Vec<String> = Vec::new();
    let mut in_type: Option<Type> = None;
    let mut in_service = false;

    for (n, raw) in text.lines().enumerate() {
        let (code, comment) = split_comment(raw);
        let at = || format!("{}:{}", path.display(), n + 1);
        if code.is_empty() {
            if in_type.is_none() && !in_service {
                // A comment block above a type is its doc; a blank line or a
                // decorative rule ends one.
                if comment.is_empty()
                    || comment
                        .chars()
                        .all(|c| "─-=".contains(c) || c.is_whitespace())
                {
                    if !comment.is_empty() {
                        continue;
                    }
                    pending_doc.clear();
                } else {
                    pending_doc.push(comment.to_string());
                }
            }
            continue;
        }
        if let Some(t) = in_type.as_mut() {
            if code == "}" {
                types.push(in_type.take().unwrap());
                pending_doc.clear();
                continue;
            }
            let (name, ty) = code
                .split_once(':')
                .unwrap_or_else(|| panic!("{}: bad field `{code}`", at()));
            let (name, optional) = match name.trim().strip_suffix('?') {
                Some(n) => (n.trim(), true),
                None => (name.trim(), false),
            };
            t.fields.push(Field {
                name: name.to_string(),
                ty: ty.trim().to_string(),
                optional,
                doc: comment.to_string(),
            });
            continue;
        }
        if in_service {
            if code == "}" {
                in_service = false;
                continue;
            }
            if let Some(v) = code.strip_prefix("version:") {
                service.version = v.trim().trim_matches('"').to_string();
                continue;
            }
            if let Some(v) = code.strip_prefix("description:") {
                service.description = v.trim().trim_matches('"').to_string();
                continue;
            }
            let (sig, result) = code
                .split_once("->")
                .unwrap_or_else(|| panic!("{}: bad method `{code}`", at()));
            let (name, params) = sig
                .trim()
                .split_once('(')
                .unwrap_or_else(|| panic!("{}: bad method `{code}`", at()));
            let params = params.trim().trim_end_matches(')');
            let params = params
                .split(',')
                .map(str::trim)
                .filter(|p| !p.is_empty())
                .map(|p| {
                    let (n, t) = p
                        .split_once(':')
                        .unwrap_or_else(|| panic!("{}: bad param `{p}`", at()));
                    (n.trim().to_string(), t.trim().to_string())
                })
                .collect();
            service.methods.push(Method {
                name: name.trim().to_string(),
                params,
                result: result.trim().to_string(),
                doc: comment.to_string(),
            });
            continue;
        }
        if let Some(rest) = code.strip_prefix("service ") {
            service.name = rest.trim_end_matches('{').trim().to_string();
            in_service = true;
            continue;
        }
        if let Some((name, rest)) = code.split_once('=')
            && rest.trim() == "{"
        {
            in_type = Some(Type {
                name: name.trim().to_string(),
                doc: pending_doc.join("\n"),
                fields: Vec::new(),
            });
            pending_doc.clear();
            continue;
        }
        panic!("{}: unrecognised line `{code}`", at());
    }
    assert!(
        !service.name.is_empty(),
        "{}: no service block",
        path.display()
    );
    (types, service)
}

fn is_int(t: &str) -> bool {
    matches!(
        t,
        "u8" | "u16" | "u32" | "u64" | "i8" | "i16" | "i32" | "i64" | "int"
    )
}

fn rust_ty(t: &str) -> String {
    if let Some(inner) = t.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
        return format!("Vec<{}>", rust_ty(inner.trim()));
    }
    match t {
        "str" | "string" => "String".into(),
        "bool" => "bool".into(),
        "f32" | "f64" | "float" => "f64".into(),
        t if is_int(t) => "i64".into(),
        other => other.to_string(),
    }
}

fn schema_of(t: &str) -> Value {
    if let Some(inner) = t.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
        return json!({ "type": "array", "items": schema_of(inner.trim()) });
    }
    match t {
        "str" | "string" => json!({ "type": "string" }),
        "bool" => json!({ "type": "boolean" }),
        "f32" | "f64" | "float" => json!({ "type": "number" }),
        t if is_int(t) => json!({ "type": "integer" }),
        other => json!({ "$ref": format!("#/components/schemas/{other}") }),
    }
}

const KEYWORDS: &[&str] = &[
    "as", "break", "const", "continue", "crate", "else", "enum", "extern", "false", "fn", "for",
    "if", "impl", "in", "let", "loop", "match", "mod", "move", "mut", "pub", "ref", "return",
    "self", "static", "struct", "super", "trait", "true", "type", "unsafe", "use", "where",
    "while", "async", "await", "dyn", "abstract", "become", "box", "do", "final", "macro",
    "override", "priv", "typeof", "unsized", "virtual", "yield", "try", "gen",
];

fn ident(name: &str) -> String {
    if KEYWORDS.contains(&name) {
        format!("r#{name}")
    } else {
        name.to_string()
    }
}

fn pascal(snake: &str) -> String {
    snake
        .split('_')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let mut c = p.chars();
            c.next()
                .map(|f| f.to_ascii_uppercase().to_string() + c.as_str())
                .unwrap_or_default()
        })
        .collect()
}

fn doc(out: &mut String, indent: &str, text: &str) {
    for line in text.lines() {
        let _ = writeln!(out, "{indent}#[doc = {:?}]", format!(" {line}"));
    }
}

const DERIVE: &str =
    "#[derive(Debug, Clone, Default, PartialEq, ::serde::Serialize, ::serde::Deserialize)]";

fn emit_fields(out: &mut String, fields: &[Field]) {
    for f in fields {
        doc(out, "        ", &f.doc);
        if f.optional {
            out.push_str("        #[serde(default, skip_serializing_if = \"Option::is_none\")]\n");
            let _ = writeln!(
                out,
                "        pub {}: Option<{}>,",
                ident(&f.name),
                rust_ty(&f.ty)
            );
        } else {
            let _ = writeln!(out, "        pub {}: {},", ident(&f.name), rust_ty(&f.ty));
        }
    }
}

fn openrpc(d: &Domain) -> Value {
    let mut schemas = serde_json::Map::new();
    for t in &d.types {
        let mut props = serde_json::Map::new();
        let mut required = Vec::new();
        for f in &t.fields {
            let mut s = schema_of(&f.ty);
            if !f.doc.is_empty() {
                s["description"] = f.doc.clone().into();
            }
            props.insert(f.name.clone(), s);
            if !f.optional {
                required.push(Value::String(f.name.clone()));
            }
        }
        let mut s = json!({ "type": "object", "properties": props, "required": required });
        if !t.doc.is_empty() {
            s["description"] = t.doc.clone().into();
        }
        schemas.insert(t.name.clone(), s);
    }
    let methods: Vec<Value> = d
        .service
        .methods
        .iter()
        .map(|m| {
            json!({
                "name": m.name,
                "description": m.doc,
                "paramStructure": "by-name",
                "params": m.params.iter().map(|(n, t)| json!({
                    "name": n, "required": true, "schema": schema_of(t)
                })).collect::<Vec<_>>(),
                "result": { "name": "result", "schema": schema_of(&m.result) },
            })
        })
        .collect();
    json!({
        "openrpc": "1.3.2",
        "info": {
            "title": d.service.name,
            "version": d.service.version,
            "description": d.service.description,
        },
        "methods": methods,
        "components": { "schemas": schemas },
    })
}

fn emit(d: &Domain) -> String {
    let by_name: BTreeMap<&str, &Type> = d.types.iter().map(|t| (t.name.as_str(), t)).collect();
    let svc = &d.service;
    let mut out = String::new();
    doc(&mut out, "", &svc.description);
    let _ = writeln!(out, "pub mod {} {{", d.module);
    out.push_str(
        "    #[allow(unused_imports)]\n    use crate::{ClientError, RequestContext, RpcError};\n\n",
    );
    let _ = writeln!(out, "    pub const SERVICE: &str = {:?};", svc.name);
    let _ = writeln!(out, "    pub const VERSION: &str = {:?};", svc.version);
    let _ = writeln!(
        out,
        "    pub const OPENRPC_JSON: &str = {:?};\n",
        openrpc(d).to_string()
    );

    for t in &d.types {
        doc(&mut out, "    ", &t.doc);
        let _ = writeln!(out, "    {DERIVE}\n    pub struct {} {{", t.name);
        emit_fields(&mut out, &t.fields);
        out.push_str("    }\n\n");
    }

    for m in &svc.methods {
        let p = pascal(&m.name);
        // Input: one field per parameter.
        let _ = writeln!(out, "    {DERIVE}");
        if m.params.is_empty() {
            out.push_str("    #[serde(default)]\n");
        }
        let _ = writeln!(out, "    pub struct {p}Input {{");
        for (n, t) in &m.params {
            let _ = writeln!(out, "        pub {}: {},", ident(n), rust_ty(t));
        }
        out.push_str("    }\n\n");
        // Output: the result type's fields, or a transparent scalar.
        let _ = writeln!(out, "    {DERIVE}\n    #[serde(default)]");
        match by_name.get(m.result.as_str()) {
            Some(t) => {
                let _ = writeln!(out, "    pub struct {p}Output {{");
                emit_fields(&mut out, &t.fields);
            }
            None => {
                let _ = writeln!(
                    out,
                    "    #[serde(transparent)]\n    pub struct {p}Output {{"
                );
                let _ = writeln!(out, "        pub value: {},", rust_ty(&m.result));
            }
        }
        out.push_str("    }\n\n");
    }

    // The server side.
    let _ = writeln!(out, "    #[::async_trait::async_trait]");
    let _ = writeln!(
        out,
        "    pub trait {}Api: Send + Sync + 'static {{",
        svc.name
    );
    for m in &svc.methods {
        let p = pascal(&m.name);
        doc(&mut out, "        ", &m.doc);
        let _ = writeln!(
            out,
            "        async fn {}(&self, ctx: &RequestContext, input: {p}Input) -> Result<{p}Output, RpcError>;",
            m.name
        );
    }
    out.push_str("    }\n\n");
    let _ = writeln!(
        out,
        "    /// Every method, with what it does, in schema order."
    );
    out.push_str("    pub const METHODS: &[(&str, &str)] = &[\n");
    for m in &svc.methods {
        let _ = writeln!(out, "        ({:?}, {:?}),", m.name, m.doc);
    }
    out.push_str("    ];\n\n");
    let _ = writeln!(
        out,
        "    /// Run `method` with `params` against `api`. `params` is the JSON-RPC params\n    /// member: an object keyed by parameter name, or null for none.\n    pub async fn dispatch<A: {}Api + ?Sized>(\n        api: &A,\n        ctx: &RequestContext,\n        method: &str,\n        params: ::serde_json::Value,\n    ) -> Result<::serde_json::Value, RpcError> {{\n        match method {{",
        svc.name
    );
    for m in &svc.methods {
        let p = pascal(&m.name);
        let _ = writeln!(
            out,
            "            {:?} => {{\n                let input: {p}Input = crate::params(params)?;\n                crate::result(&api.{}(ctx, input).await?)\n            }}",
            m.name, m.name
        );
    }
    out.push_str(
        "            other => Err(RpcError::method_not_found(other)),\n        }\n    }\n\n",
    );

    // The client side.
    let client = format!("{}Client", svc.name);
    let _ = writeln!(
        out,
        "    /// A typed client for the {} domain, at its full RPC endpoint URL.",
        d.module
    );
    let _ = writeln!(
        out,
        "    #[derive(Debug, Clone)]\n    pub struct {client} {{\n        url: String,\n    }}\n"
    );
    let _ = writeln!(out, "    impl {client} {{");
    out.push_str("        /// A client for the endpoint at `url` (`<root>/api/<domain>/rpc`).\n");
    out.push_str("        pub fn new_at(url: &str) -> Result<Self, ClientError> {\n            Ok(Self { url: url.to_string() })\n        }\n\n");
    for m in &svc.methods {
        let p = pascal(&m.name);
        doc(&mut out, "        ", &m.doc);
        let _ = writeln!(
            out,
            "        pub async fn {}(&self, input: {p}Input) -> Result<{p}Output, ClientError> {{\n            crate::client::call(&self.url, {:?}, &input).await\n        }}\n",
            m.name, m.name
        );
    }
    out.push_str("    }\n}\n\n");
    out
}

fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("oschema");
    println!("cargo:rerun-if-changed={}", root.display());
    let mut dirs: Vec<PathBuf> = std::fs::read_dir(&root)
        .expect("oschema/")
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.is_dir())
        .collect();
    dirs.sort();
    let mut out = String::from("// Generated by build.rs from oschema/. Do not edit.\n\n");
    for dir in dirs {
        let module = dir.file_name().unwrap().to_string_lossy().into_owned();
        let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
            .unwrap()
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.extension().is_some_and(|e| e == "oschema"))
            .collect();
        files.sort();
        for f in &files {
            println!("cargo:rerun-if-changed={}", f.display());
        }
        let mut types = Vec::new();
        let mut service = None;
        for f in &files {
            let (t, s) = parse(f);
            types.extend(t);
            assert!(service.is_none(), "{}: a second service block", f.display());
            service = Some(s);
        }
        let domain = Domain {
            module,
            types,
            service: service.unwrap_or_else(|| panic!("{}: no service", dir.display())),
        };
        out.push_str(&emit(&domain));
    }
    let dest = PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("api.rs");
    std::fs::write(dest, out).unwrap();
}
