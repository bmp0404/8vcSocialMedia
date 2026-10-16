test "judge output parses into rubric" {
    let result = baml.json.from_string<PostGrade>(`{
        "verdict": "Revise", "confidence": "Low",
        "relevance": {"score": 3, "reason": "Fits mission"},
        "clarity": {"score": 4, "reason": "Readable"},
        "specificity": {"score": 3, "reason": "Concrete feature"},
        "engagement_potential": {"score": 2, "reason": "Limited comparison"},
        "grounding": {"score": 4, "reason": "Supported by context"},
        "comparisons": [
            {"reference_id": "good", "observation": "Specific release detail"},
            {"reference_id": "poor", "observation": "Avoids vague announcement"}
        ],
        "strengths": ["Clear benefit"], "improvements": ["Show a concrete example"],
        "limitations": ["No audience-size normalization"]
    }`);
    assert.equal(result.verdict, Verdict.Revise);
    assert.equal(result.clarity.score, 4);
    assert.equal(result.comparisons.length(), 2)
}


test "Claude Code judge has no harness tools" {
    let judge = judge_client("claude_code", "sonnet");
    if let cli: claude_code.ClaudeCodeClient = judge {
        assert.equal(cli.model, "sonnet");
        assert.equal(cli.harness_tools.length(), 0);
        assert.equal(cli.permission_mode, "default")
    } else {
        assert.is_true(false)
    }
}
