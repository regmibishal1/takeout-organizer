cask "takeout-organizer" do
  version "0.1.0"
  sha256 "0d10211a6019f83adb228b10a5897843d92a8d60a975838bf0ce6adf56e7ad89"

  url "https://github.com/regmibishal1/takeout-organizer/releases/download/v#{version}/Takeout-Organizer-#{version}-macos-arm64.zip"
  name "Takeout Organizer"
  desc "Fix the dates and locations in a Google Photos Takeout"
  homepage "https://github.com/regmibishal1/takeout-organizer"

  depends_on arch: :arm64
  depends_on :macos

  app "Takeout Organizer.app"
  binary "#{appdir}/Takeout Organizer.app/Contents/MacOS/takeout-organizer"

  zap trash: "~/Library/Application Support/takeout-organizer"

  caveats <<~EOS
    Takeout Organizer is not notarized. The first time, right-click it in
    Applications and choose Open, or install with --no-quarantine.
  EOS
end
