Put service binaries here by hand (varsha downloads nothing until Phase 5).
Versions are read from the folder names, so keep the version number in each folder name.
Any number of versions can sit side by side:

  bin\apache\<apache-folder>\bin\httpd.exe          e.g. httpd-2.4.68-260617-Win64-VS18
  bin\php\<php-folder>\php.exe, php-cgi.exe         e.g. php-8.3.33-Win32-vs16-x64, php-8.5.12-nts-...
  bin\mysql\<mysql-folder>\bin\mysqld.exe           e.g. mysql-8.4.3-winx64
  bin\mariadb\<mariadb-folder>\bin\mariadbd.exe     e.g. mariadb-11.8.9-winx64
  bin\mailpit\mailpit.exe
  bin\composer\composer.phar

Every database version is its own service in config\services.json, with its own port and
data folder, so several versions can run at the same time.

Click "Rescan bin" in varsha after adding or removing binaries.
