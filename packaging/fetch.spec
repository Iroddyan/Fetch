Name:           fetch
Version:        1.1.0
Release:        1%{?dist}
Summary:        Native desktop frontend for yt-dlp
License:        MIT
BuildArch:      noarch
Source0:        %{name}-%{version}.tar.gz
Requires:       python3
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       yt-dlp
Requires:       ffmpeg
Requires:       ffmpeg-libs

%description
Fetch is a native Linux desktop frontend for yt-dlp with format controls,
playlist selection, a persistent queue, dependency status, and logs.

%prep
%autosetup -n %{name}-%{version}

%build

%install
install -d %{buildroot}%{_bindir}
install -d %{buildroot}%{_datadir}/fetch
install -d %{buildroot}%{_datadir}/applications
install -d %{buildroot}%{_datadir}/metainfo
install -d %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
cp -a fetch %{buildroot}%{_datadir}/fetch/
install -m 0755 packaging/fetch-launcher %{buildroot}%{_bindir}/fetch
install -m 0644 com.iroddyan.fetch.desktop %{buildroot}%{_datadir}/applications/
install -m 0644 packaging/com.iroddyan.fetch.metainfo.xml %{buildroot}%{_datadir}/metainfo/com.iroddyan.fetch.metainfo.xml
install -m 0644 icons/hicolor/scalable/apps/com.iroddyan.fetch.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/

%files
%{_bindir}/fetch
%{_datadir}/fetch
%{_datadir}/applications/com.iroddyan.fetch.desktop
%{_datadir}/metainfo/com.iroddyan.fetch.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/com.iroddyan.fetch.svg
