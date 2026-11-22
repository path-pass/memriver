# Source this before any cargo command: it uses only a toolchain that is already
# installed (never downloads one) and keeps cargo's home inside the repository.
export RUSTUP_AUTO_INSTALL=0
_repo="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
export CARGO_HOME="$_repo/.cargo-home"
_toolchain="$(find "${RUSTUP_HOME:-$HOME/.rustup}/toolchains" -maxdepth 1 -name '1.100.0-*' 2>/dev/null | head -n 1)"
if [ -n "$_toolchain" ]; then
  export PATH="$_toolchain/bin:$PATH"
fi
unset _repo _toolchain
