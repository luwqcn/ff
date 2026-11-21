;;; ff-parse.el --- 把 ff.org 解析成 feeds.json（旧格式，已停用）  -*- lexical-binding: t; -*-
;;
;; ⚠️ 已停用：只支持早期「二级标题 + :LINK: 属性」格式，不从标题里的 Org 链接拆主页。
;; 现行格式是「二级标题 ** [[博客主页][源名]] + 一级标题下写分类描述」，请用 ff_parse.py。
;; 本文件仅作历史对照保留，ff_run.sh 和 CI 都不再调用它。
;;
;; 用法：
;;   FF_SOURCE=/path/to/ff.org FF_OUT=/path/to/feeds.json \
;;     emacs --batch -q -l ff-parse.el
;;
;; 解析规则：
;;   一级标题 -> 分组名
;;   二级标题 -> 一个订阅源；URL / LINK / ENABLED / INTERVAL 从属性抽屉读取
;;   LINK 是博客主页（页面源名链过去），不写则回退到 URL（feed 地址）

(require 'org-element)
(require 'json)
(require 'md5)
(require 'url-parse)

(defvar ff--category "未分类")

(defun ff--trim (s)
  (when (and (stringp s) (> (length (string-trim s)) 0))
    (string-trim s)))

(defun ff--bool (s)
  (not (member (downcase (or s "yes")) '("no" "false" "0" "off" "disabled"))))

(defun ff--slug (url)
  (let* ((host (ignore-errors (url-host (url-generic-parse-url url))))
         (clean (replace-regexp-in-string "[^a-z0-9]" "" (downcase (or host "feed")))))
    (when (> (length clean) 12)
      (setq clean (substring clean 0 12)))
    (format "%s-%s" (if (string-empty-p clean) "feed" clean)
            (substring (md5 url) 0 6))))

(defun ff-parse-file (file)
  (with-temp-buffer
    (insert-file-contents file)
    (org-mode)
    (setq ff--category "未分类")
    (delq nil
          (org-element-map (org-element-parse-buffer) 'headline
            (lambda (hl)
              (save-excursion
                (goto-char (org-element-property :begin hl))
                (let ((level (org-element-property :level hl))
                      (title (org-element-property :raw-value hl)))
                  (cond
                   ((= level 1)
                    (setq ff--category title)
                    nil)
                   ((= level 2)
                    (let ((url (ff--trim (org-entry-get nil "URL")))
                          (blog-link (ff--trim (org-entry-get nil "LINK"))))
                      (when url
                        (let* ((interval (ff--trim (org-entry-get nil "INTERVAL")))
                               ;; heading 下第一段正文当作订阅源的简介
                               (raw-desc (org-element-map
                                          hl 'paragraph
                                          (lambda (p) (org-element-interpret-data p))
                                          nil t))
                               (desc (if raw-desc
                                         (ff--trim (substring-no-properties raw-desc))
                                       "")))
                          `((slug . ,(ff--slug url))
                            (title . ,title)
                            (url . ,url)
                            (link . ,(or blog-link url))
                            (category . ,ff--category)
                            (description . ,desc)
                            (enabled . ,(ff--bool (org-entry-get nil "ENABLED")))
                            (interval . ,(if interval
                                             (max 600 (or (ignore-errors
                                                            (string-to-number interval))
                                                          14400))
                                           14400)))))))
                   (t nil)))))))))

(let ((src (getenv "FF_SOURCE"))
      (out (getenv "FF_OUT")))
  (unless (and src out)
    (error "需要设置 FF_SOURCE 和 FF_OUT 环境变量"))
  (let ((payload `((source . ,src)
                   (parsed_at . ,(format-time-string "%Y-%m-%dT%H:%M:%S%z"))
                   (feeds . ,(ff-parse-file src)))))
    (with-temp-file out
      (insert (json-encode payload))
      (insert "\n")))
  (message "ff-parse: 已解析 %s" src))
