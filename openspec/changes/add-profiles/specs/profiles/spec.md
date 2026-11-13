## ADDED Requirements

### Requirement: Profiles with optional passwords
The system SHALL let each person using the app have a profile with a unique name and an optional password. It SHALL store a password only as a salted scrypt hash, and SHALL treat a profile with no password as one anyone can open.

#### Scenario: A profile with a password
- **WHEN** the admin adds the profile "Ana" with a password
- **THEN** the database holds the name and a salted hash, and never the password itself

#### Scenario: A profile without a password
- **WHEN** the admin adds the profile "Ana" with the password field left blank
- **THEN** anyone can open Ana's profile from the sign-in page with one click, and the Profiles page notes that it has no password

### Requirement: Sign in and switch profiles
When there's more than one profile, or the only profile has a password, the system SHALL ask which profile to use before showing any page. The sign-in page SHALL list the profiles by name, open a profile without a password on a click, and ask for the password of a profile that has one. The system SHALL remember the profile in the browser for a year, and SHALL show the current profile's name in the nav with **Switch profile** and **Sign out**.

#### Scenario: Sign in with a password
- **WHEN** the user picks "Dana" on the sign-in page and enters Dana's password
- **THEN** the app opens Dana's playlists, and later visits from that browser open Dana's profile without asking

#### Scenario: Wrong password
- **WHEN** the user enters a wrong password
- **THEN** the page waits about a second, says the password is wrong, and stays on the sign-in page

#### Scenario: Switch profile
- **WHEN** the user clicks **Switch profile** in the nav
- **THEN** the sign-in page opens, and picking another profile opens that profile's playlists

#### Scenario: A partial page after the sign-in ends
- **WHEN** a part of a page refreshes itself after the browser's profile was signed out
- **THEN** the whole browser page goes to the sign-in page, instead of showing the sign-in page inside the part

### Requirement: One-person installs need no sign-in
While there's exactly one profile and it has no password, the system SHALL open that profile without showing the sign-in page.

#### Scenario: Upgrade of a one-person install
- **WHEN** an install with no profiles starts the new version
- **THEN** it creates the profile "Me" as an admin with no password, and every page opens as before with no sign-in page

### Requirement: The admin manages profiles and app settings
The system SHALL make the first profile an admin, and SHALL let only admins open the Settings page, set the Spotify Client ID, and manage profiles: add, rename, clear a password, make an admin or not, and remove. It SHALL always keep at least one admin. Every profile SHALL be able to change its own name and password.

#### Scenario: Not an admin
- **WHEN** a profile that isn't an admin opens the Settings page
- **THEN** the page says only an admin can change settings, and the nav doesn't offer it

#### Scenario: Forgotten password
- **WHEN** the admin clears Ana's password on the Profiles page
- **THEN** Ana's profile opens with one click, and Ana can set a new password

#### Scenario: The last admin
- **WHEN** the only admin tries to remove their own admin role or their profile
- **THEN** the page refuses and says the app needs at least one admin

#### Scenario: Remove a profile
- **WHEN** the admin removes Ana's profile and confirms by typing "Ana"
- **THEN** Ana's playlists, service connections, and favorites history leave the app, and the playlists' files stay on disk

### Requirement: Password changes sign out other browsers
The system SHALL sign every browser out of a profile when its password is changed or cleared, or when the profile is removed.

#### Scenario: A changed password
- **WHEN** Dana changes Dana's password on a laptop
- **THEN** a phone that was signed in as Dana goes to the sign-in page on its next request, and the laptop stays signed in

### Requirement: Requests from other sites are refused
The system SHALL refuse a POST request whose `Origin` header names a different host than the request's, and SHALL send the profile cookie with `SameSite=Lax` and `HttpOnly`, and `Secure` when the request came over HTTPS.

#### Scenario: A form on another site
- **WHEN** a page on another site posts a form to `/playlists/<id>/remove`
- **THEN** the app answers 403 and changes nothing

### Requirement: The app password stays in front
When `APP_PASSWORD` is set, the system SHALL ask for it before the sign-in page, as it does without profiles.

#### Scenario: Both passwords
- **WHEN** `APP_PASSWORD` is set and there are two profiles
- **THEN** the browser asks for the app password first, and then the sign-in page asks which profile
