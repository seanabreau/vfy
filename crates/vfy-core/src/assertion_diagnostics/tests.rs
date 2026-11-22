use super::render_assertion_explanation;
use serde_json::json;

#[test]
fn nested_calls_and_siblings_keep_where_grouping() {
    let value = |text| json!({"kind":"value", "value":text});
    let call = |result, arguments| {
        json!({"kind":"call", "value":result,
        "function": value("f"), "arguments":arguments, "keywords":[]})
    };
    let first = call(
        "1",
        vec![json!({"unpack":false, "expression":
        call("1", vec![json!({"unpack":false, "expression":value("1")})])})],
    );
    let second = call("2", vec![json!({"unpack":false, "expression":value("2")})]);
    let comparison = json!({"kind":"comparison", "selected":0, "operators":["=="],
        "operands":[first, second]});
    assert_eq!(
        render_assertion_explanation(None, &comparison.to_string()).expect("valid comparison"),
        "assert 1 == 2\n +  where 1 = f(1)\n +    where 1 = f(1)\n +  and   2 = f(2)"
    );
}

#[test]
fn invalid_comparison_shapes_are_rejected() {
    let comparison = json!({"kind":"comparison", "selected":1, "operators":["=="],
        "operands":[{"kind":"value","value":"1"}, {"kind":"value","value":"2"}]});
    assert!(render_assertion_explanation(None, &comparison.to_string()).is_err());
}
