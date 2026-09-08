"""OpenAPI JSON fixtures only; HTTP connections and API execution are mocked."""
import copy
import json
import unittest
from unittest import mock
from urllib.parse import urlparse

from desk import connectors, openapi_import as api, tools


def spec(operation=None, path="/items/{item-id}", method="get"):
    return {"openapi": "3.1.0", "info": {"title": "Fixture", "version": "1"},
            "servers": [{"url": "https://api.example.invalid/v1"}],
            "paths": {path: {method: operation or {"operationId": "getItem", "summary": "Read item",
                "parameters": [{"name": "item-id", "in": "path", "required": True, "schema": {"type": "integer"}},
                               {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "OK"}}}}}}


class ParseTests(unittest.TestCase):
    def test_get_draft_is_compatible_with_existing_runtime_and_renames_path_args(self):
        doc = spec()
        draft = api.build_connector(json.dumps(doc), "getItem")
        draft = connectors._validate(draft)
        self.assertEqual(draft["url_template"], "https://api.example.invalid/v1/items/{path_item_id}?q={query_q}")
        with mock.patch.object(tools, "_fetch", return_value="fixture") as fetch:
            tools._run_http_connector(draft, {"path_item_id": 17, "query_q": "a/b & c"}, "read")
        self.assertEqual(fetch.call_args.args[:2], ("GET", "https://api.example.invalid/v1/items/17?q=a%2Fb%20%26%20c"))
        self.assertFalse(draft["enabled"])

    def test_path_alias_replacement_does_not_rewrite_previously_inserted_alias(self):
        operation = {"operationId": "aliases", "parameters": [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in ("id", "path_id")]}
        draft = api.build_connector(json.dumps(spec(operation, "/{id}/{path_id}")), "aliases")
        self.assertEqual(draft["url_template"], "https://api.example.invalid/v1/{path_id}/{path_path_id}")

    def test_operation_overrides_path_parameter_and_server(self):
        doc = spec()
        item = doc["paths"]["/items/{item-id}"]
        item["parameters"] = [{"name": "q", "in": "query", "required": False, "schema": {"type": "boolean"}}]
        item["servers"] = [{"url": "https://wrong.example.invalid"}]
        item["get"]["servers"] = [{"url": "/chosen/{version}", "variables": {"version": {"default": "v2"}}}]
        result = api.prepare(spec_text=json.dumps(doc), source_url="https://spec.example.invalid/docs/openapi.json")
        self.assertFalse(result["questions"])
        self.assertEqual(result["selected"]["params"]["query_q"]["type"], "string")
        self.assertTrue(result["selected"]["url_template"].startswith("https://spec.example.invalid/chosen/v2/"))

    def test_post_flat_required_json_keeps_string_escaping_and_integer_type(self):
        operation = {"operationId": "create", "requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object", "required": ["text", "count", "{literal}"], "properties": {
                "text": {"type": "string"}, "count": {"type": "integer"}, "{literal}": {"type": "string"}}}}}}}
        draft = api.build_connector(json.dumps(spec(operation, "/items", "post")), "create")
        with mock.patch.object(tools, "_fetch", return_value="fixture") as fetch:
            tools._run_http_connector(draft, {"body_text": 'quote" and\nline', "body_count": 3, "body__literal_": "literal"}, "workspace")
        self.assertEqual(json.loads(fetch.call_args.args[2]), {"text": 'quote" and\nline', "count": 3, "{literal}": "literal"})

    def test_security_placeholders_and_anonymous_operation_override(self):
        doc = spec()
        doc["components"] = {"securitySchemes": {"token": {"type": "http", "scheme": "bearer"}, "key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}}
        doc["security"] = [{"token": [], "key": []}]
        result = api.prepare(spec_text=json.dumps(doc))
        self.assertEqual(result["selected"]["headers"], {"Authorization": "Bearer ${OPENAPI_TOKEN}", "X-API-Key": "${OPENAPI_KEY}"})
        self.assertFalse(result["questions"])
        self.assertTrue(any("OPENAPI_TOKEN" in warning for warning in result["warnings"]))
        doc["paths"]["/items/{item-id}"]["get"]["security"] = []
        self.assertEqual(api.prepare(spec_text=json.dumps(doc))["selected"]["headers"], {})

    def test_unsupported_auth_and_schema_are_not_silently_imported(self):
        cases = []
        for schema in ({"type": "boolean"}, {"type": "number"}, {"type": "array", "items": {"type": "string"}}, {"type": "string", "enum": ["a"]}):
            doc = spec()
            doc["paths"]["/items/{item-id}"]["get"]["parameters"][1]["schema"] = schema
            cases.append(doc)
        doc = spec(); doc["paths"]["/items/{item-id}"]["get"]["parameters"][1]["required"] = False; cases.append(doc)
        for scheme in ({"type": "http", "scheme": "basic"}, {"type": "oauth2", "flows": {}}, {"type": "apiKey", "in": "query", "name": "key"}):
            doc = spec(); doc["components"] = {"securitySchemes": {"auth": scheme}}; doc["security"] = [{"auth": []}]; cases.append(doc)
        cases.append(spec({"operationId": "delete"}, "/items", "delete"))
        for doc in cases:
            with self.subTest(doc=doc):
                result = api.prepare(spec_text=json.dumps(doc))
                self.assertNotIn("selected", result)
                self.assertFalse(result["operations"][0]["supported"])
                self.assertTrue(result["questions"])

    def test_optional_body_nested_and_multiple_content_types_rejected(self):
        base = {"operationId": "create", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"x": {"type": "object"}}, "required": ["x"]}}}}}
        cases = [base]
        optional = copy.deepcopy(base); optional["requestBody"]["required"] = False; cases.append(optional)
        mixed = copy.deepcopy(base); mixed["requestBody"]["content"]["application/xml"] = {}; cases.append(mixed)
        for operation in cases:
            self.assertFalse(api.inspect_spec(json.dumps(spec(operation, "/items", "post")))["operations"][0]["supported"])

    def test_bounded_local_refs_and_external_refs_never_fetch(self):
        doc = spec()
        doc["components"] = {"parameters": {"query": {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}}}
        doc["paths"]["/items/{item-id}"]["get"]["parameters"][1] = {"$ref": "#/components/parameters/query"}
        with mock.patch.object(api, "fetch_spec", side_effect=AssertionError("Do not fetch references")):
            self.assertTrue(api.prepare(spec_text=json.dumps(doc))["selected"])
            doc["components"]["parameters"]["query"] = {"$ref": "#/components/parameters/query"}
            self.assertIn("순환", api.prepare(spec_text=json.dumps(doc))["questions"][0])
            doc["components"]["parameters"]["query"] = {"$ref": "https://private.invalid/query.json"}
            self.assertIn("외부 $ref", api.prepare(spec_text=json.dumps(doc))["questions"][0])

    def test_yaml_oversize_duplicate_keys_and_unsupported_versions_are_explicit(self):
        for text in ("openapi: 3.1.0\npaths: {}", json.dumps({"openapi": "2.0"}), '{"openapi":"3.1.0","paths":{},"paths":{}}', "x" * (api.MAX_SPEC_BYTES + 1)):
            with self.assertRaises(api.SpecError):
                api.inspect_spec(text)

    def test_duplicate_operation_ids_and_multiple_operations_need_selection(self):
        doc = spec()
        doc["paths"]["/second"] = {"get": {"operationId": "getItem"}}
        result = api.inspect_spec(json.dumps(doc))
        self.assertTrue(all(not row["supported"] for row in result["operations"]))
        doc["paths"]["/second"]["get"]["operationId"] = "second"
        result = api.prepare(spec_text=json.dumps(doc))
        self.assertNotIn("selected", result)
        self.assertTrue(result["questions"])
        self.assertIn("selected", api.prepare(spec_text=json.dumps(doc), operation_id="second"))

    def test_unusable_servers_are_not_guessed_or_contacted(self):
        for url in ("/relative", "http://localhost/v1", "http://127.0.0.1/v1", "https://user:secret@example.invalid/v1", "https://example.invalid/v1?token=secret"):
            doc = spec(); doc["servers"] = [{"url": url}]
            self.assertFalse(api.inspect_spec(json.dumps(doc))["operations"][0]["supported"])


class FetchTests(unittest.TestCase):
    def response(self, content=b"{}", status=200, headers=None):
        response = mock.Mock()
        response.status = status
        response.length = None
        response.fp = None
        response.getheader.side_effect = lambda key, default=None: (headers or {}).get(key, default)
        response.read1.side_effect = [content, b""]
        return response

    def connection(self, response):
        connection = mock.Mock()
        connection.getresponse.return_value = response
        return connection

    def test_redirect_is_revalidated_and_spec_get_is_the_only_request(self):
        first = self.connection(self.response(status=302, headers={"Location": "https://cdn.example.invalid/openapi.json"}))
        last = self.connection(self.response(json.dumps(spec()).encode()))
        with mock.patch.object(tools, "_destinations", side_effect=lambda url, **kwargs: (urlparse(url), ["pinned"])) as destinations, mock.patch.object(tools, "_pinned_connection", side_effect=[first, last]), mock.patch.object(api.threading, "Timer"):
            result = api.fetch_spec("https://spec.example.invalid/spec")
        self.assertEqual(result["source_url"], "https://cdn.example.invalid/openapi.json")
        self.assertEqual([call.args[0] for call in destinations.call_args_list], ["https://spec.example.invalid/spec", "https://cdn.example.invalid/openapi.json"])
        self.assertTrue(all(call.kwargs["allow_local"] is False for call in destinations.call_args_list))
        self.assertEqual(first.request.call_args.args[0], "GET")
        self.assertEqual(last.request.call_args.args[0], "GET")
        first.close.assert_called_once(); last.close.assert_called_once()

    def test_redirect_private_target_rejection_never_opens_second_connection(self):
        first = self.connection(self.response(status=302, headers={"Location": "http://127.0.0.1/private"}))
        with mock.patch.object(tools, "_destinations", side_effect=[(urlparse("https://spec.example.invalid/spec"), ["pinned"]), RuntimeError("private URL blocked")]), mock.patch.object(tools, "_pinned_connection", return_value=first) as connect, mock.patch.object(api.threading, "Timer"):
            with self.assertRaisesRegex(RuntimeError, "private URL blocked"):
                api.fetch_spec("https://spec.example.invalid/spec")
        connect.assert_called_once()
        first.close.assert_called_once()

    def test_fetch_rejects_oversize_instead_of_truncating_json(self):
        connection = self.connection(self.response(b"x" * (api.MAX_SPEC_BYTES + 1)))
        with mock.patch.object(tools, "_destinations", return_value=(urlparse("https://spec.example.invalid/spec"), ["pinned"])), mock.patch.object(tools, "_pinned_connection", return_value=connection), mock.patch.object(api.threading, "Timer"):
            with self.assertRaisesRegex(api.SpecError, "1 MiB"):
                api.fetch_spec("https://spec.example.invalid/spec")
        connection.close.assert_called_once()

    def test_prepare_fetches_only_when_text_not_supplied(self):
        with mock.patch.object(api, "fetch_spec", return_value={"text": json.dumps(spec()), "source_url": "https://spec.example.invalid/spec"}) as fetch:
            self.assertIn("selected", api.prepare(source_url="https://spec.example.invalid/spec"))
            fetch.assert_called_once()
            api.prepare(spec_text=json.dumps(spec()), source_url="https://spec.example.invalid/spec")
            self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
