# Pin for VS Code

Syntax highlighting for [Pin](https://github.com/pin-language/pin) (`.pn` files): keywords, types
(`list<int>`, `map<string,int>`), variables, strings with `$var` / `{$var}` interpolation, comments,
numbers, built-in functions and operators. Also comment toggling, bracket matching and auto-indent.

## Install

From a clone of the Pin repository:

```bash
cd editors/vscode
npx @vscode/vsce package          # makes pin-0.5.0.vsix
code --install-extension pin-0.5.0.vsix
```

Then reload VS Code and open any `.pn` file.
