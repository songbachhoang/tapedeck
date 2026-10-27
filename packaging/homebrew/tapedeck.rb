# Homebrew formula for Tapedeck. It goes in a tap repo: github.com/songbachhoang/homebrew-tap/Formula/tapedeck.rb
# On each release, update `url` to the new tag and `sha256` to:
#   curl -sL https://github.com/songbachhoang/tapedeck/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256
class Tapedeck < Formula
  desc "Save YouTube audio and video on your Mac, with no ads and nothing uploaded"
  homepage "https://github.com/songbachhoang/tapedeck"
  url "https://github.com/songbachhoang/tapedeck/archive/refs/tags/v0.2.0.tar.gz"
  sha256 "88704aba05b27ae228dae631d8b3dce8b57f80f21b2932d0b7f2f5a6d6e293a8"
  license "MIT"
  revision 1

  depends_on "deno"
  depends_on "ffmpeg"
  depends_on "python@3.15"

  def install
    libexec.install "tapedeck.py", "web"
    (bin/"tapedeck").write <<~SH
      #!/bin/bash
      exec "#{formula_opt_bin("python@3.15")}/python3.15" "#{libexec}/tapedeck.py" "$@"
    SH
  end

  service do
    run [opt_bin/"tapedeck", "--no-browser"]
    keep_alive true
    # launchd starts services with a bare PATH; ffmpeg and deno live in Homebrew's bin.
    environment_variables PATH: std_service_path_env
    log_path var/"log/tapedeck.log"
    error_log_path var/"log/tapedeck.log"
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/tapedeck --version")
  end
end
