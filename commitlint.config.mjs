// Conventional Commits, read by the commit-msg hook (.pre-commit-config.yaml)
// and by the commitlint job in .github/workflows/commitlint.yml.
export default {
	extends: ['@commitlint/config-conventional'],
};
