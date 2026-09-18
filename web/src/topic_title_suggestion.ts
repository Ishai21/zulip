import {$} from "jquery";
import * as z from "zod/mini";

import * as banners from "./banners.ts";
import type {AlertBanner} from "./banners.ts";
import * as buttons from "./buttons.ts";
import * as channel from "./channel.ts";
import {$t} from "./i18n.ts";
import * as ui_report from "./ui_report.ts";

export const topic_title_suggestion_schema = z.object({
    stream_id: z.number(),
    topic_name: z.string(),
    suggested_title: z.string(),
    reason: z.string(),
    message_id: z.number(),
});

export type TopicTitleSuggestion = z.infer<typeof topic_title_suggestion_schema>;

const check_response_schema = z.object({
    suggestion: z.nullable(topic_title_suggestion_schema),
});

let current_suggestion: TopicTitleSuggestion | undefined;
// Suggestions the user dismissed this session; the server re-sends a
// pending suggestion whenever they post in the topic again.
const dismissed = new Set<string>();

function suggestion_key(suggestion: TopicTitleSuggestion): string {
    return `${suggestion.stream_id}:${suggestion.topic_name}:${suggestion.suggested_title}`;
}

function $banner_container(): JQuery {
    return $("#navbar_alerts_wrapper");
}

export function handle_event(suggestion: TopicTitleSuggestion): void {
    if (dismissed.has(suggestion_key(suggestion))) {
        return;
    }
    show_suggestion(suggestion);
}

export function show_suggestion(suggestion: TopicTitleSuggestion): void {
    current_suggestion = suggestion;
    const banner: AlertBanner = {
        process: "topic-title-suggestion",
        intent: "info",
        label: $t(
            {
                defaultMessage:
                    'The topic "{topic_name}" seems to have drifted. Suggested title: "{suggested_title}" — {reason}',
            },
            suggestion,
        ),
        buttons: [
            {
                variant: "solid",
                label: $t({defaultMessage: "Rename topic"}),
                custom_classes: "accept-topic-title-suggestion",
            },
            {
                variant: "text",
                label: $t({defaultMessage: "Dismiss"}),
                custom_classes: "dismiss-topic-title-suggestion",
            },
        ],
        close_button: true,
    };
    banners.open(banner, $banner_container());
}

function show_no_drift_notice(topic_name: string): void {
    const banner: AlertBanner = {
        process: "topic-title-suggestion",
        intent: "success",
        label: $t(
            {defaultMessage: 'The title "{topic_name}" still fits the conversation.'},
            {topic_name},
        ),
        buttons: [],
        close_button: true,
    };
    banners.open_and_close(banner, $banner_container(), 5000);
}

export function request_suggestion(stream_id: number, topic_name: string): void {
    void channel.post({
        url: "/json/topics/title_suggestion",
        data: {stream_id, topic: topic_name},
        success(raw_data) {
            const data = check_response_schema.parse(raw_data);
            if (data.suggestion === null) {
                show_no_drift_notice(topic_name);
            } else {
                show_suggestion(data.suggestion);
            }
        },
        error(xhr) {
            ui_report.error(
                $t({defaultMessage: "Could not check this topic's title."}),
                xhr,
                $("#home-error"),
            );
        },
    });
}

function accept_suggestion($button: JQuery): void {
    if (current_suggestion === undefined) {
        return;
    }
    const suggestion = current_suggestion;
    const $banner = $button.closest(".banner");
    buttons.show_button_loading_indicator($button);
    // Renaming via the standard message edit API with propagate_mode
    // "change_all" moves every message in the topic, exactly as the
    // manual "Move topic" dialog does.
    void channel.patch({
        url: "/json/messages/" + suggestion.message_id,
        data: {
            topic: suggestion.suggested_title,
            propagate_mode: "change_all",
            send_notification_to_old_thread: false,
            send_notification_to_new_thread: true,
        },
        success() {
            current_suggestion = undefined;
            banners.close($banner);
        },
        error(xhr) {
            buttons.hide_button_loading_indicator($button);
            ui_report.error($t({defaultMessage: "Failed to rename topic."}), xhr, $("#home-error"));
        },
    });
}

export function initialize(): void {
    $banner_container().on("click", ".accept-topic-title-suggestion", function (this: HTMLElement) {
        accept_suggestion($(this));
    });
    $banner_container().on("click", ".dismiss-topic-title-suggestion", function (this: HTMLElement) {
        if (current_suggestion !== undefined) {
            dismissed.add(suggestion_key(current_suggestion));
        }
        current_suggestion = undefined;
        banners.close($(this).closest(".banner"));
    });
}
