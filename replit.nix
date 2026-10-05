{ pkgs }: {
  deps = [
    pkgs.python311
    pkgs.python311Packages.pip
    pkgs.ffmpeg-full
    pkgs.fontconfig
    pkgs.poppins
    pkgs.libsndfile
  ];
}
