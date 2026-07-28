-- index ix_audit_log_timestamp on audit_log
CREATE INDEX ix_audit_log_timestamp ON audit_log (timestamp);

-- index ix_documents_content_hash on documents
CREATE INDEX ix_documents_content_hash ON documents (content_hash);

-- index ix_users_username on users
CREATE UNIQUE INDEX ix_users_username ON users (username);

-- table assignments on assignments
CREATE TABLE assignments (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	role_id INTEGER NOT NULL, 
	scope_type VARCHAR(12) NOT NULL, 
	scope_id INTEGER, 
	effect VARCHAR(6) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(role_id) REFERENCES roles (id)
);

-- table audit_log on audit_log
CREATE TABLE audit_log (
	id INTEGER NOT NULL, 
	actor_id INTEGER, 
	actor_name VARCHAR(160) NOT NULL, 
	action VARCHAR(60) NOT NULL, 
	object_type VARCHAR(40) NOT NULL, 
	object_id VARCHAR(40) NOT NULL, 
	ip VARCHAR(64) NOT NULL, 
	user_agent VARCHAR(300) NOT NULL, 
	details TEXT NOT NULL, 
	timestamp DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(actor_id) REFERENCES users (id)
);

-- table cabinets on cabinets
CREATE TABLE cabinets (
	id INTEGER NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	parent_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(parent_id) REFERENCES cabinets (id)
);

-- table doc_classes on doc_classes
CREATE TABLE doc_classes (
	id INTEGER NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	parent_id INTEGER, 
	PRIMARY KEY (id), 
	UNIQUE (name), 
	FOREIGN KEY(parent_id) REFERENCES doc_classes (id)
);

-- table doc_fts on doc_fts
CREATE VIRTUAL TABLE doc_fts USING fts5(
                        document_id UNINDEXED,
                        title,
                        content,
                        tokenize='porter unicode61'
                    );

-- table doc_fts_config on doc_fts_config
CREATE TABLE 'doc_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;

-- table doc_fts_content on doc_fts_content
CREATE TABLE 'doc_fts_content'(id INTEGER PRIMARY KEY, c0, c1, c2);

-- table doc_fts_data on doc_fts_data
CREATE TABLE 'doc_fts_data'(id INTEGER PRIMARY KEY, block BLOB);

-- table doc_fts_docsize on doc_fts_docsize
CREATE TABLE 'doc_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);

-- table doc_fts_idx on doc_fts_idx
CREATE TABLE 'doc_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;

-- table doc_metadata on doc_metadata
CREATE TABLE doc_metadata (
	id INTEGER NOT NULL, 
	document_id INTEGER NOT NULL, 
	"key" VARCHAR(80) NOT NULL, 
	value VARCHAR(500) NOT NULL, 
	confidence FLOAT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id)
);

-- table doc_versions on doc_versions
CREATE TABLE doc_versions (
	id INTEGER NOT NULL, 
	document_id INTEGER NOT NULL, 
	version_no INTEGER NOT NULL, 
	file_key VARCHAR(300) NOT NULL, 
	filename VARCHAR(300) NOT NULL, 
	content_type VARCHAR(120) NOT NULL, 
	size INTEGER NOT NULL, 
	checksum VARCHAR(64) NOT NULL, 
	ocr_text TEXT NOT NULL, 
	created_by INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

-- table documents on documents
CREATE TABLE documents (
	id INTEGER NOT NULL, 
	folder_id INTEGER NOT NULL, 
	title VARCHAR(300) NOT NULL, 
	class_id INTEGER, 
	class_confidence FLOAT, 
	content_hash VARCHAR(64) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	ocr_status VARCHAR(20) NOT NULL, 
	ocr_confidence FLOAT, 
	language VARCHAR(16) NOT NULL, 
	page_count INTEGER NOT NULL, 
	created_by INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(folder_id) REFERENCES folders (id), 
	FOREIGN KEY(class_id) REFERENCES doc_classes (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

-- table dup_groups on dup_groups
CREATE TABLE dup_groups (
	id INTEGER NOT NULL, 
	primary_document_id INTEGER NOT NULL, 
	similarity_type VARCHAR(12) NOT NULL, 
	resolved BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(primary_document_id) REFERENCES documents (id)
);

-- table dup_members on dup_members
CREATE TABLE dup_members (
	id INTEGER NOT NULL, 
	dup_group_id INTEGER NOT NULL, 
	document_id INTEGER NOT NULL, 
	similarity_score FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(dup_group_id) REFERENCES dup_groups (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id)
);

-- table folders on folders
CREATE TABLE folders (
	id INTEGER NOT NULL, 
	cabinet_id INTEGER NOT NULL, 
	parent_id INTEGER, 
	name VARCHAR(160) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(cabinet_id) REFERENCES cabinets (id), 
	FOREIGN KEY(parent_id) REFERENCES folders (id)
);

-- table permissions on permissions
CREATE TABLE permissions (
	id INTEGER NOT NULL, 
	code VARCHAR(40) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code)
);

-- table role_permissions on role_permissions
CREATE TABLE role_permissions (
	id INTEGER NOT NULL, 
	role_id INTEGER NOT NULL, 
	permission_id INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (role_id, permission_id), 
	FOREIGN KEY(role_id) REFERENCES roles (id), 
	FOREIGN KEY(permission_id) REFERENCES permissions (id)
);

-- table roles on roles
CREATE TABLE roles (
	id INTEGER NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	description VARCHAR(255) NOT NULL, 
	is_system BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

-- table users on users
CREATE TABLE users (
	id INTEGER NOT NULL, 
	username VARCHAR(80) NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	email VARCHAR(200) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	mfa_enabled BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (email)
);
